"""Byte-level regression cases recorded with the Python station layout and mesh at 538."""
import hashlib
import json
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import busstations as bs
import busstationmesh as bm


def fingerprint(value):
    h = hashlib.sha256()

    def write(data):
        h.update(struct.pack("<Q", len(data)))
        h.update(data)

    def visit(v):
        write(type(v).__name__.encode())
        if isinstance(v, np.ndarray):
            write(str(v.dtype).encode())
            write(str(v.shape).encode())
            write(v.tobytes())
        elif isinstance(v, (list, tuple, dict, set)):
            contents = v.items() if isinstance(v, dict) else sorted(v) if isinstance(v, set) else v
            for x in contents:
                visit(x)
            write(b"end")
        elif isinstance(v, float):
            write(struct.pack("<d", v))
        else:
            write(str(v).encode("utf-8"))

    visit(value)
    return h.hexdigest()


def case(seed, name):
    rng = np.random.default_rng(seed)
    path = Path(__file__).parent / "data" / "busstations" / (name + ".json")
    items = bs.items_from_json(json.loads(path.read_text(encoding="utf-8"))["items"])
    # Preserve adjacency while varying projected distances, orientation and inclusion at tile seams.
    matrix = rng.uniform(-0.1, 0.1, (2, 2)) + np.eye(2)
    offset = rng.uniform(-10, 10, 2)
    for it in items:
        x, y = it["xy"].T
        it["xy"] = np.column_stack((x * matrix[0, 0] + y * matrix[1, 0],
                                    x * matrix[0, 1] + y * matrix[1, 1])) + offset
    stations = bs.find(items)
    ground = bm.DrawnGround(lambda p: p[:, 0] * 0.012 - p[:, 1] * 0.007)
    box = (-150, -120, rng.uniform(20, 150), rng.uniform(20, 150))
    origin = (box[0], box[1], 1.0)
    return {
        "layout": fingerprint([vars(st) for st in stations]),
        "furniture": fingerprint([(bs.sign_spot(s), bs.board_spot(s), bs.facade_spot(s),
                                    bm.crossings(s), bm.bollard_points(s), bm.kiosk_spot(s)) for s in stations]),
        "paint": fingerprint([fn(stations, box) for fn in (bm.line_chunks, bm.lane_chunks, bm.edge_chunks,
                                                            bm.crossing_chunks, bm.zigzag_chunks)]),
        "mesh": fingerprint(bm.mesh(None, box, stations, ground, origin, overlap=bool(seed % 2))),
        "collision": fingerprint(bm.collision(None, box, stations, ground, origin)),
    }


def geometry_cases():
    rng = np.random.default_rng(545)
    result = []
    for i in range(256):
        n = (1, 2, 7, 8, 9, 15, 17, 31, 128, 129)[i % 10]
        xy = rng.normal(size=(n, 2)).cumsum(0) * 2.25
        if i % 3 == 0:
            xy = xy[::-1]
        if i % 4 == 0 and n > 1:
            xy[-1] = xy[0]
        if i % 5 == 0 and n > 3:
            xy[1] = xy[2]
        pts = np.vstack((xy[0], xy[-1], rng.normal(size=(9, 2))))
        result.append(fingerprint((bs.area(xy), bs.centroid(xy), bs.hull(xy), bs.path_dist(xy, pts),
                                   bs.pad_ring(xy, 1.75), bm._obb(xy), bm._gable(xy), bm._sd_ring(xy, pts),
                                   bm._segment_quads(xy, 2.25), bm._lane_frames({"xy": xy}, 1.25))))
    return fingerprint(result)


def feature_cases():
    import mapgeom

    class Frame:
        def enu_to_geodetic(self, x, y, z):
            return y / 100000, x / 100000, z

        def geodetic_to_enu(self, lat, lon, height):
            calls.append((lat.copy(), lon.copy()))
            return lon * 100000, lat * 100000, np.zeros_like(lat)

    rng = np.random.default_rng(14545)
    result = []
    for n in range(1, 33):
        coords = rng.uniform(-0.003, 0.003, (n * 3, 2))
        tags = ['{" A ": "b","a": "C"}', '{"u": "Straße", "N": 1, "F": false}', 'null', '{', '[]',
                '{"k":"one","K":"two","k":"three"}', '{"a": ["x", null], "B": {"Y": 1}}']
        features = mapgeom.Features("buildings", rng.integers(0, n // 2 + 1, n).astype(str),
                                    rng.integers(1, 4, n, dtype=np.uint8), np.arange(n + 1), np.arange(n + 1),
                                    np.arange(n + 1) * 3, coords, attrs={
                                        "class": rng.choice(["roof", "transportation", "  None "], n),
                                        "height": rng.choice([2.5, -0.0, np.nan, np.inf], n).astype(np.float32),
                                        "name": rng.choice([" A ", "nan", "Термінал"], n),
                                        "tags": rng.choice(tags, n)})
        calls = []
        layers = {"buildings": [features, features]}
        first = bs.items_from_layers(Frame(), layers, (17, -22), 135)
        features.attrs["class"] = np.full(n, "shelter")
        second = bs.items_from_layers(Frame(), layers, (17, -22), 155)
        result.append(fingerprint((first, second, calls)))
    return fingerprint(result)


class NativeParity(unittest.TestCase):
    def test_clockwise_roof_measures_span_in_mapped_order(self):
        # Tied bounding rectangles on this clockwise footprint differ by one bit
        # in their computed span. Version 538 takes the first edge in mapped order.
        ring = np.array([[-48.67363503160104, 9.757372016190834], [-39.25298380645563, -13.033549125206621],
                         [-93.61401361186992, -35.503737971866855], [-103.03466483701533, -12.712816830469404]])
        st = bs.Station()
        st.canopies.append({"ring": ring, "src": "roof", "area": bs.area(ring), "flat": True})
        mesh = bm._Mesh()
        bm.roofs(None, bm.DrawnGround(lambda xy: np.zeros(len(xy))), st, mesh)
        self.assertEqual(np.concatenate(mesh.P)[:, 2].max().hex(), "0x1.843b4f214466dp+2")

    def test_two_point_ring_has_negative_zero_on_its_endpoints(self):
        # The closing edge's computed intercept can round just past the first point.
        # A two-point ring still has no interior, even at zero distance.
        segment = np.array([[0.0, 0.0], [1.0, 49.0]])
        self.assertEqual(bm._sd_ring(segment, segment).tobytes(), struct.pack("<dd", -0.0, -0.0))

    def test_geometry(self):
        self.assertEqual(geometry_cases(), "937dda76a34bd9bddf56f0f11e08af326306d81ff12acb2f4ad08537cec14300")

    def test_feature_batches(self):
        self.assertEqual(feature_cases(), "4b87f97bc34f435aa8acdbe8efd6d61e4f6783df3a7bbb3bf2d691cd3e26075a")

    def test_randomized_stations(self):
        saved_ids = bs.PASSAGE_IDS.copy()
        self.addCleanup(bs.PASSAGE_IDS.update, saved_ids)
        self.addCleanup(bs.PASSAGE_IDS.clear)
        for (seed, name), expected in GOLDEN.items():
            with self.subTest(seed=seed, station=name):
                actual = case(seed, name)
                for part, digest in expected.items():
                    with self.subTest(part=part):
                        self.assertEqual(actual[part], digest)


GOLDEN = {(34, 'hk_terminal'): {'layout': 'ee7edb23769395be651ff4fa34763885314d3e695fbb6877d012ca1fe5fc1b10',
                       'furniture': '630dcd371c57551e57c2cf4e4ab4bd058b120128b9d59003113086d58f6d7943',
                       'paint': '96d4be50e109165e96ba9ad64311651a4051178e4296fe127013e9d1d30e0a5a',
                       'mesh': '2af4161c61a8d89a9375072083f741e752ff262a98d5b8b44892c0dd5d2f2d73',
                       'collision': '080312323236e31fc50fd51a1751969063daf058bb4de65c93cd0cd859ce2e06'},
 (85, 'brno_zvonarka'): {'layout': '9ccc9e543a3d3290f5ecf52cfb8ae698a8ed79cd45f542bab3a1741f69ea351b',
                         'furniture': '96e0a7bc1557c85178d11b0ac85d2b08a759a604ca577c703751cbcceefd0143',
                         'paint': '01f24e6fd5c39bf4d246170668461575be862fadea3e08069af05331f6835244',
                         'mesh': 'c0013b19896c471bfb4dabf4aa2d97c27357eb792badd63aaca953a7ec3a4488',
                         'collision': '1f466af24f1460b0cf98e50adc730341473400e60efe9befb4e880c8ef660277'},
 (991, 'nachod'): {'layout': '0a65b3138884db7f57f962887eb3ec060dd9409c4fa93b31310825358ad8562c',
                   'furniture': '15f0a24f8bf802b37ae1055d0386f6925c97a0478f5c7a5ab8ce014e93eb8573',
                   'paint': '24dfe0a5d4de4b9a50223caa22fe73707dfb958939b91676556c985f6debc776',
                   'mesh': 'ef0cc03d6236b7a6fb6c1ef5c6dc095534f1fb8a9e1bac988758a4f523cfe72b',
                   'collision': '968a01e7b547becafdc63368ebb10ce4f2c82337bdaedc33f539d0bde592a57a'},
 (1101, 'vin_west'): {'layout': 'f60f0bd05b2c4fb8e7ca95cdb4d206c9e6314d224bc1ef46f79d2432f4915c2e',
                      'furniture': '2639f334b6c2434dcaf1d2da2e0ba300685db65b1c45038fa71af189fe31c61c',
                      'paint': '1b21742d4ae9b5f925463f8fcaa66769686d69b8b26061ff4c2338a493c384f0',
                      'mesh': '0b6c76dc4c30afd52b2f9cdc71ebf0d0e55154338a4f0b220e7dc7bb1b4b35e3',
                      'collision': '8cc90218bd4d91e4a4b7fbeb65d39e4fa21919fa98122f04dbd1895679299834'},
 (82, 'london_victoria'): {'layout': '5c20ad3548ca2b1af0c6ce872efa5798dcdd56dbba4fb3cd08390a4d5d2678fe',
                           'furniture': '0b1f7c64898d9d63a99481d8b1b3e531c4452ef6836f8e82dd43fd718eae6cd3',
                           'paint': '0c9b96e501c79b23456c53940f844f9a27c3b0620064cada3fb31a510247f0bd',
                           'mesh': '6dffe09c8750062e4cdabfb1e6f660c4bc716dbce4ea86affd7f3614e7917f06',
                           'collision': '9170bce9e00c6b6207dd19f0339cebc0c60221b725b2b6cbec72035dba4ed22f'},
 (84, 'rakovnik'): {'layout': '8ae52fb7053d197879ee100557a9b0ccfd6bb58d5ffea6968fc5e754ee2f369e',
                    'furniture': '535c22ad5e7befa74970400c2c0c4c9a2048eb8619ac2a5d10d5696c051bc109',
                    'paint': 'e17caae207fbe38b619ae5c8a1c03b84c5f4068e02412068e2e27efb4952f67e',
                    'mesh': 'ed2263507fd2f007b5e6c76221c14f6f9ae5784124fc4d816fbd86f0cd73d3f3',
                    'collision': '6031a68ccaf4a8eb6ef15d826b0ddde8f2d838dd9ecc7319edb7c5c4f66e8910'},
 (2, 'vin3'): {'layout': '44adc6337ee1459ef82124d00698da158c4a4b68dd908b3bf9a0755b4a1c2423',
               'furniture': '44adc6337ee1459ef82124d00698da158c4a4b68dd908b3bf9a0755b4a1c2423',
               'paint': 'c43cce1aea29103a5249f26c13e934328080a96a1fe5adc07c6a5e3969bb8483',
               'mesh': '6b8abd708f0c9c751ca28a2e12ccf88deb50e233e06c01c759906698741a025a',
               'collision': 'e71ad28996402b50f14708bc3b75f0d309ea53715fdd67b0d2277b5b32cea210'},
 (1006, 'wien_erdberg'): {'layout': '086d7b48803ccbe2d3acfa9b8df5aa36659fcf6185c3796ac4a963721acb9715',
                          'furniture': '4a808923ec5670baac1f1a1ded86200963ee40f504abc48aef53bfbba41d05fd',
                          'paint': '72d0e6902ffc9fde751e05ada2d898b0b13a0b3aa5cffce7c20619462d21ec57',
                          'mesh': '68425f65000997b39f5bf423bee4021b8ff47a1fbd6c7c0ba79791d9e1c30d64',
                          'collision': 'c6d45b97d4757a5929da43344802ef56ba0b607f6d71e55ee2089b1ccb2e2fcf'}}
