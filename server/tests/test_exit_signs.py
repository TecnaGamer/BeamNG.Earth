"""OSM exit-sign placement against the current 1.6.0 road geometry."""
import sys
import copy
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import exitsigns
import geodesy
import roads
import signroads as roadcontext
import signroads as roadlanes


FRAME = geodesy.EnuFrame(41.7, -85.0)


def ll(x, y):
    lat, lon, _ = FRAME.enu_to_geodetic(x, y, 0.0)
    return [float(lon), float(lat)]


def fixture():
    main_spec = roads.Spec(11.0, "paved", False, "motorway", True, 2.5, 1, 110.0)
    ramp_spec = roads.Spec(6.0, "paved", False, "motorway", True, 2.5, 1, 55.0)
    main_line = np.array([[-1900., 0., 0.], [256., 0., 0.]])
    ramp_line = np.array([[0., 0., 0.], [100., -120., 0.], [200., -200., 0.]])
    rows = [("main", main_spec, main_line, -85.0), ("ramp", ramp_spec, ramp_line, -85.0)]
    obj = {
        "id": -201,
        "tags": {"highway": "motorway_junction", "ref": "36"},
        "exit": {
            "label": {"ref": "36", "names": ["Auburn"], "routes": []},
            "approach": [ll(-1900., 0.), ll(0., 0.)],
            "ramp": [ll(0., 0.), ll(100., -120.), ll(200., -200.)],
            "main_ids": [100], "ramp_id": 201, "side": 1,
        },
    }
    return rows, obj


def test_advance_and_gore_signs_follow_existing_road_geometry():
    rows, obj = fixture()
    flat = lambda lat, lon: np.zeros_like(np.asarray(lat, dtype=float))
    signs = exitsigns.place(FRAME, [obj], rows, (-1800., -300., 256., 100.), flat,
                            link_ids={"ramp"})
    advance = [s["distance"] for s in signs if s["kind"] == "advance"]
    assert advance == ["1 MILE", "1/2 MILE", "1/4 MILE"]
    gore = [s for s in signs if s["kind"] == "ramp"]
    assert len(gore) == 1
    assert gore[0]["label"]["ref"] == "36"
    assert np.isfinite(gore[0]["centre"]).all()

    mesh = exitsigns.build(signs, (0., 0., 0.))
    assert mesh is not None
    positions, triangles, uvs, materials, material_indices = mesh
    assert positions.ndim == 2 and positions.shape[1] == 3
    assert triangles.ndim == 2 and triangles.shape[1] == 3
    assert uvs.shape[0] == positions.shape[0]
    assert "bne_exit_letters" in materials
    assert material_indices.max() < len(materials)


def test_index_records_uses_real_destination_tags_and_skips_entries():
    coords = {1: ll(-500., 0.), 2: ll(-200., 0.), 3: ll(0., 0.),
              4: ll(30., -25.), 5: ll(60., -60.), 6: ll(-350., 30.)}
    main = [(10, [1, 2, 3], {"highway": "motorway", "oneway": "yes"})]
    ramps = [
        (20, [3, 4, 5], {"highway": "motorway_link", "oneway": "yes", "destination": "Auburn;Angola"}),
        (21, [6, 2], {"highway": "motorway_link", "oneway": "yes", "destination": "entry only"}),
    ]
    tags = {3: {"highway": "motorway_junction", "ref": "36"}}
    rows = exitsigns.index_records(main, ramps, coords, tags)
    assert len(rows) == 1
    assert rows[0]["ways"][0]["label"] == {"ref": "36", "names": ["Auburn", "Angola"], "routes": []}


class ExitSignsTest(unittest.TestCase):
    def test_existing_sign_mesh_and_advance_distances(self):
        test_advance_and_gore_signs_follow_existing_road_geometry()

    def test_existing_osm_index_labels_and_entries(self):
        test_index_records_uses_real_destination_tags_and_skips_entries()

    def test_one_small_panel_across_all_tiles_of_the_same_gore(self):
        rows, obj = fixture()
        flat = lambda la, lo: np.zeros_like(np.asarray(la, float))
        boxes = [(-1800., -300., 0., 100.), (0., -300., 64., 100.),
                 (64., -300., 128., 100.), (128., -300., 256., 100.)]
        signs = [s for box in boxes for s in exitsigns.place(FRAME, [obj, obj], rows, box, flat,
                                                            link_ids={'ramp'})]
        self.assertEqual(len([s for s in signs if s['kind'] == 'ramp']), 1,
                         'later tiles must not search for another place down the same ramp')
        self.assertEqual(sorted(s['distance'] for s in signs if s['kind'] == 'advance'),
                         ['1 MILE', '1/2 MILE', '1/4 MILE'])
        whole = exitsigns.place(FRAME, [obj], rows, (-1800., -300., 256., 100.), flat, link_ids={'ramp'})
        self.assertEqual(sorted((s['kind'], s['centre']) for s in signs),
                         sorted((s['kind'], s['centre']) for s in whole))

    def test_entire_panel_clears_the_actual_generated_pavement_on_left_and_right_exits(self):
        for side in (1, -1):
            rows, obj = fixture()
            obj = copy.deepcopy(obj)
            if side < 0:
                rows = [(fid, sp, line*np.array([1., -1., 1.]), lon) for fid, sp, line, lon in rows]
                ramp = rows[1][2]
                obj['exit']['ramp'] = [ll(x, y) for x, y, _ in ramp]
                obj['exit']['side'] = side
            pavements = []
            for fid, sp, line, _ in rows:
                sp = roadlanes.spec(sp, link=fid == 'ramp')
                p, width = roadlanes.pavement(sp, line, FRAME)
                pavements.append((fid, sp, p, width))
            signs = exitsigns.place(FRAME, [obj], (), (-1800., -300., 256., 300.),
                                   lambda la, lo: np.zeros_like(la), pavements=pavements)
            gore, = [s for s in signs if s['kind'] == 'ramp']
            polygons = [exitsigns._outline(line, width) for _, _, line, width in pavements]
            self.assertTrue(exitsigns._clear(np.array(gore['centre'][:2]),
                                            gore['width']/2+exitsigns.GORE_CLEAR_M, polygons))
            self.assertGreater(gore['travel'][0], .99, 'panel faces the approaching highway traffic')

    def test_source_context_claims_one_welcome_center_sign_across_tiles(self):
        from test_roads import FRAME as frame, features, flat, rules
        f = features([[(-1900., 100.), (700., 100.)], [(0., 100.), (100., -20.), (200., -100.)]],
                     ['motorway', 'motorway'])
        f.rules['oneway'] = rules([(0, 1.), (1, 1.)], 'float')
        f.rules['subclass'] = rules([(1, 'link')])
        ramp = f.parts(1)[0][0]
        obj = dict(id=-20, exit=dict(label=dict(ref='', names=['Welcome Center'], routes=[]),
                    approach=[f.parts(0)[0][0][0].tolist(), ramp[0].tolist()], ramp=ramp.tolist(),
                    main_ids=[10], ramp_id=20, side=1))
        context = roadcontext.Context(lambda key: f)
        ribbons, mains = exitsigns.source_pavements(frame, obj, context)
        signs = [s for tx in range(-8,4) for ty in (-1,0)
                 for s in exitsigns.place(frame,[obj],(),roads.tile_bbox(tx,ty),flat,
                                           pavements=ribbons,ramp_mains=mains)]
        gore, = [s for s in signs if s['kind'] == 'ramp']
        self.assertEqual(gore['style'], 'rest_area')
        self.assertEqual(gore['label']['names'], ['Welcome Center'])
        self.assertIs(exitsigns.source_pavements(frame,obj,context)[0],ribbons)

    def test_distinct_osm_exits_keep_their_own_small_panels(self):
        rows, first = fixture()
        first = copy.deepcopy(first)
        second = copy.deepcopy(first)
        second['id'], second['exit']['ramp_id'] = -202, 202
        second['exit']['main_ids'] = [101]
        for key in ('approach', 'ramp'):
            xy = exitsigns._xy(FRAME, second['exit'][key])+[600., 0.]
            second['exit'][key] = [ll(x, y) for x, y in xy]
        pavements = []
        for obj, offset, mid, rid in ((first, 0., 100, 201), (second, 600., 101, 202)):
            for k, (_, sp, line, _) in enumerate(rows):
                sp = roadlanes.spec(sp, link=k == 1)
                p, width = roadlanes.pavement(sp, line+[offset, 0., 0.], FRAME)
                pavements.append((f'osm:w{rid if k else mid}:segment', sp, p, width))
        signs = exitsigns.place(FRAME, [first, second], (), (-1800., -300., 900., 100.),
                               lambda la, lo: np.zeros_like(la), pavements=pavements)
        self.assertEqual({s['id'] for s in signs if s['kind'] == 'ramp'}, {-201, -202})

    def test_narrow_split_does_not_fall_back_to_a_second_shoulder_panel(self):
        rows, obj = fixture()
        rows[1][2][:, 1] *= .025
        obj['exit']['ramp'] = [ll(x, y) for x, y, _ in rows[1][2]]
        signs = exitsigns.place(FRAME, [obj], rows, (-1800., -300., 256., 100.),
                               lambda la, lo: np.zeros_like(la), link_ids={'ramp'})
        self.assertFalse(any(s['kind'] == 'ramp' for s in signs))


if __name__ == '__main__':
    unittest.main()
