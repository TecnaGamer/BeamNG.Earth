"""The requested A Collins Road correction must not affect other roads."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from test_roads import features, LAT, LON, flat
import roads, roadoverrides, furniture, surface, geodesy, mapgeom

ROOT = Path(__file__).resolve().parents[2]
FRAME = geodesy.EnuFrame(36.93660, -86.80461, 0.)


def target(name='A Collins Road', shift=(0., 0.)):
    f = features([[(-100., 20.), (400., 20.)]], ['residential'])
    f.lonlat += [FRAME.lon0 - LON + shift[0], FRAME.lat0 - LAT + shift[1]]
    f.attrs['name'] = np.array([name])
    f.attrs['lane_count'] = np.array([1.])
    return f


class RoadOverrideTest(unittest.TestCase):
    def test_cached_full_road_is_gravel_and_source_data_is_retained(self):
        data = json.loads((Path(__file__).parent / 'fixtures/a_collins_road.json').read_text(encoding='utf-8'))
        f = mapgeom.build('roads', np.array([data['id']]), [(mapgeom.LINE, [[np.array(data['line'])]])])
        f.attrs = {k: np.array([v]) for k, v in data['attrs'].items()}
        sp = roads.road_specs(f)[0]
        self.assertEqual((sp.kind, sp.surf, sp.material, sp.width, sp.lanes), ('gravel', 'gravel', 'gravel_road', 5.7, 1))
        self.assertFalse(sp.utility_poles)
        self.assertFalse(roads.has_sidewalks(sp))
        self.assertEqual(f.rules, {})
        self.assertEqual(f.attrs['osm_surface'][0], 'gravel')
        # Source splits retain the override, independent of feature IDs and direction.
        line = np.array(data['line'])
        for part in (line[:len(line)//2+1], line[len(line)//2:][::-1]):
            split = mapgeom.build('roads', np.array(['new-id']), [(mapgeom.LINE, [[part]])])
            split.attrs = dict(f.attrs)
            self.assertEqual(roads.road_specs(split)[0].material, 'gravel_road')

    def test_match_requires_both_name_and_location(self):
        for name, shift, match in (('A Collins Road', (0, 0), True), ('A. Collins Rd.', (0, 0), True),
                                   ('A Collins Road', (1, 0), False), ('Chandlers Road', (0, 0), False)):
            sp = roads.road_specs(target(name, shift))[0]
            self.assertEqual(sp.material == 'gravel_road', match, (name, shift))
            self.assertEqual(sp.utility_poles, not match)

    def test_full_gravel_physics_photo_texture_and_no_asphalt_mask(self):
        f = target()
        e, n = np.meshgrid(np.arange(32., 64., .25), np.arange(4., 36., .25))
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.)
        mat = surface.paint_roads(np.full(e.shape, surface.GRASS, np.uint8), la, lo, [f], .25)
        self.assertEqual(mat[64, 64], surface.GRAVEL_ROAD)
        self.assertEqual(surface.MATERIALS[surface.GRAVEL_ROAD][1], 'GRAVEL')
        fake = SimpleNamespace(urban=None, roads=lambda *_: ([f], None))
        self.assertIsNone(surface.road_mask(FRAME, 32., 4., 32., 128, fake))
        image = np.full((128, 128, 3), 200, np.uint8)
        photo = surface.photo_roads(image, FRAME, 32., 4., 32., [f])
        self.assertGreater(int(photo[64, 64, 0]), int(photo[64, 64, 2]) + 15)
        for in_ground in (False, True):
            with patch.object(roads, 'ROADS_IN_GROUND', in_ground):
                chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
            self.assertTrue(any(c['m'] == 'bne_road_gravel' for c in chunks))
            self.assertFalse(any(c['m'].startswith('line_') or c['m'] == roads.LOOK['paved']['m'] for c in chunks))
            for c in chunks:
                if c['m'] == 'bne_road_gravel':
                    self.assertLessEqual(c['w'], 2.1, 'stone grain is not stretched across the whole road')

    def test_poles_and_wires_are_omitted_without_removing_neighbouring_utilities(self):
        f = target()
        sp = roads.road_specs(f)[0]
        line = np.c_[np.arange(-100., 401., 50.), np.full(11, 20.), np.full(11, 10.)]
        row = ('collins', sp, line, FRAME.lon0)
        for town in (False, True):
            wires, props, walks = [], [], []
            roads.build_tile(FRAME, 0, 0, [f], flat, props=props, walks=walks, wires=wires, town=town)
            self.assertFalse(wires)
            self.assertFalse(any(p[0] in (furniture.G_POLE, furniture.G_POLELAMP) for p in props))
            other = ('neighbour', sp._replace(utility_poles=True), line + [0, 80, 0], FRAME.lon0)
            wires = []
            props = furniture.place([row, other], [], (0, 0, 256, 256), wires=wires, town=town)
            self.assertTrue(wires)
            poles = [p for p in props if p[0] == furniture.G_POLE]
            self.assertTrue(poles)
            self.assertTrue(all(p[2] > 80 for p in poles))

    def test_gravel_materials_and_runtime_look_are_packaged_with_matching_physics(self):
        base = ROOT / 'mod/beamng_earth/levels/beamng_earth/art'
        mats = json.loads((base / 'terrains/main.materials.json').read_text())
        ground = mats['bngEarthGroundGravelRoad']
        self.assertEqual(ground['groundmodelName'], 'GRAVEL')
        self.assertIn('t_gravel_b.png', ground['baseColorDetailTex'])
        self.assertEqual(len({m['persistentId'] for m in mats.values()}), len(mats))
        mats = json.loads((base / 'shapes/beamngEarth/tunnel.materials.json').read_text())
        self.assertEqual(mats['bne_road_gravel']['groundType'], 'GRAVEL')
        self.assertIn('t_gravel_b.png', mats['bne_road_gravel']['Stages'][0]['baseColorMap'])


if __name__ == '__main__':
    unittest.main()
