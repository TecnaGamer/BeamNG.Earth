"""Sweden's bus stop signs (sebusstops.py)."""
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import mapgeom
import roads
import sebusstops
import signstream


def features(frame, stops):
    """An infrastructure layer of points: stops = [(fid, x, y, class, name, tags)]."""
    xy = np.array([[s[1], s[2]] for s in stops], np.float64).reshape(-1, 2)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    off = np.arange(len(stops) + 1, dtype=np.int64)
    return mapgeom.Features(
        layer="infrastructure", ids=np.array([s[0] for s in stops]), kind=np.full(len(stops), mapgeom.POINT, np.uint8),
        part_off=off, ring_off=off, coord_off=off, lonlat=np.column_stack((lon, lat)),
        attrs={"subtype": np.array(["transit"] * len(stops)), "class": np.array([s[3] for s in stops]),
               "name": np.array([s[4] for s in stops], object),
               "tags": np.array([json.dumps(s[5]) for s in stops])})


class Map:
    def __init__(self, feats):
        self.feats = feats

    def cell(self, key, layers=(), kind=None):
        return type("Cell", (), {"layers": {"infrastructure": self.feats}})()


class Roads:
    """A sign context with one road along x at y, 7 m wide."""
    def __init__(self, y):
        self.y = y

    def pavements(self, frame, points, left_hand=False):
        return [("rd", None, np.array([[-5000.0, self.y, 0.0], [5000.0, self.y, 0.0]]), 7.0)]


class KindTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(57.7, 11.97, 0.0)

    def test_a_stop_is_the_pole_or_the_halt_on_the_road(self):
        f = features(self.frame, [("a", 0, 0, "bus_stop", "Brunnsparken", {"highway": "bus_stop", "shelter": "no"}),
                                  ("b", 0, 0, "bus_stop", None, {"public_transport": "stop_position"}),
                                  ("c", 0, 0, "amenity", None, {"highway": "bus_stop"}),
                                  ("d", 0, 0, "bench", "Bänk", {})])
        self.assertEqual([sebusstops.is_stop(f, i) for i in range(4)], [True, True, True, False])

    def test_name_and_lage(self):
        f = features(self.frame, [("a", 0, 0, "bus_stop", " Sahlgrenska  Huvudentré ", {"local_ref": "a"}),
                                  ("b", 0, 0, "bus_stop", None, {"local_ref": "mot centrum"}),
                                  ("c", 0, 0, "bus_stop", "nan", {}),
                                  ("d", 0, 0, "bus_stop", "Heden", {"ref": "e"}),
                                  ("e", 0, 0, "bus_stop", "Heden", {"ref": "14017"})])
        self.assertEqual(sebusstops.label(f, 0), ("Sahlgrenska Huvudentré", "A"))
        self.assertEqual(sebusstops.label(f, 1), ("", ""))          # (a local_ref that is not a letter or a number)
        self.assertEqual(sebusstops.label(f, 2), ("", ""))
        self.assertEqual(sebusstops.label(f, 3), ("Heden", "E"))    # (Västtrafik's letters are in ref)
        self.assertEqual(sebusstops.label(f, 4), ("Heden", ""))     # (a stop's number is not its letter)

    def test_only_sweden(self):
        self.assertTrue(sebusstops.wanted("se"))
        self.assertFalse(sebusstops.wanted("NO"))
        self.assertFalse(sebusstops.wanted(None))


class PoleTest(unittest.TestCase):
    near = [("rd", type("S", (), {"width": 7.0})(), np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]))]

    def test_at_the_kerb_on_the_stops_side_ahead_of_it(self):
        x, y, gx, gy = sebusstops.pole(50.0, -5.0, self.near)       # south of a road east: its traffic goes east
        self.assertAlmostEqual(y, -(3.5 + sebusstops.KERB_M))
        self.assertAlmostEqual(x, 50.0 + sebusstops.AHEAD_M)
        self.assertEqual((gx, gy), (1.0, 0.0))
        x, y, gx, gy = sebusstops.pole(50.0, 2.0, self.near)        # north (a point on the carriageway too): west
        self.assertAlmostEqual(y, 3.5 + sebusstops.KERB_M)
        self.assertAlmostEqual(x, 50.0 - sebusstops.AHEAD_M)
        self.assertEqual(gx, -1.0)

    def test_a_halt_on_the_road_takes_the_platforms_side(self):
        north = [np.array([[40.0, 5.0], [60.0, 5.0]])]
        x, y, gx, gy = sebusstops.pole(50.0, 0.0, self.near, north, "a")
        self.assertAlmostEqual(y, 3.5 + sebusstops.KERB_M)
        self.assertAlmostEqual(x, 50.0)                             # beside the halt
        self.assertEqual(gx, -1.0)
        south = [np.array([[50.0, -6.0]])]
        self.assertLess(sebusstops.pole(50.0, 0.2, self.near, south, "a")[1], 0.0)
        far = [np.array([[90.0, 5.0], [99.0, 5.0]])]                # another stop's platform: a side by the id
        sides = {sebusstops.pole(50.0, 0.0, self.near, far, fid)[1] > 0.0 for fid in "abcdefgh"}
        self.assertEqual(sides, {True, False})

    def test_a_halt_on_a_one_way_road_stands_on_its_right(self):
        for d, want in ((1, -1.0), (-1, 1.0)):
            near = [("rd", type("S", (), {"width": 7.0, "oneway": True, "dir": d})(), self.near[0][2])]
            for fid in "abcd":
                y = sebusstops.pole(50.0, 0.0, near, (), fid)[1]
                self.assertAlmostEqual(y, want * (3.5 + sebusstops.KERB_M))

    def test_none_far_from_every_road(self):
        self.assertIsNone(sebusstops.pole(50.0, 80.0, self.near))
        self.assertIsNone(sebusstops.pole(50.0, 5.0, []))

    def test_none_on_a_bridge(self):
        found = [("a", 50.0, -5.0, "Bron", "")]
        deck = [{"deck": True, "n": np.array([[0.0, 0.0], [100.0, 0.0]]), "w": 9.0}]
        self.assertEqual(len(sebusstops.rows_of(found, self.near)), 1)
        self.assertEqual(sebusstops.rows_of(found, self.near, deck), [])

    def test_a_stop_mapped_twice_has_one_pole(self):
        found = [("a", 50.0, -5.0, "Torget", "A"), ("b", 56.0, 0.0, "Torget", "A"), ("c", 56.0, 5.0, "Torget", "A")]
        south = [np.array([[40.0, -5.5], [60.0, -5.5]])]
        got = sebusstops.rows_of(found, self.near, (), south)
        self.assertEqual(len(got), 2)                               # the south side's once, the north side's own
        self.assertEqual(sorted(g[1] > 0 for g in got), [False, True])


class PictureTest(unittest.TestCase):
    def test_e22_is_blue_with_a_black_bus_on_white(self):
        im = sebusstops.e22_picture(64)
        self.assertEqual(im.shape, (64, 64, 3))
        self.assertEqual(tuple(im[6, 32]), sebusstops.BLUE)          # the field
        self.assertEqual(tuple(im[14, 32]), sebusstops.WHITE)        # the white square
        self.assertEqual(tuple(im[34, 32]), sebusstops.BLACK)        # the bus's body under its windows
        self.assertTrue(np.array_equal(im, sebusstops.e22_picture(64)))


class TileTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(57.7, 11.97, 0.0)
        self.box = roads.tile_bbox(0, 0)
        self.x0, self.yc = self.box[0] + 60.0, (self.box[1] + self.box[3]) / 2.0     # the road along the tile's middle

    def tile(self, stops, country="SE", tx=0, ty=0):
        stops = [(s[0], s[1], s[2] + self.yc, *s[3:]) for s in stops]
        with tempfile.TemporaryDirectory() as out:
            mesh, faces = sebusstops.sign_tile(Map(features(self.frame, stops)), self.frame, tx, ty,
                                               lambda la, lo: np.full(np.shape(la), 12.0), out, "/v", Roads(self.yc), (),
                                               country)
            return mesh, faces, sorted(os.listdir(out))

    def test_a_named_stop_has_e22_and_its_name_on_both_sides(self):
        mesh, faces, files = self.tile([("a", self.x0, -5.0, "bus_stop", "Brunnsparken", {"local_ref": "B"})])
        p, t, uv, materials, mi = mesh
        self.assertEqual(len(materials), 3)                         # metal, E22, the name
        self.assertEqual(sorted(faces), sorted(materials[1:]))
        self.assertEqual(files, sorted(m + ".color.dds" for m in materials[1:]))
        for m in (1, 2):                                            # each face twice: one from each way
            self.assertEqual(int((np.asarray(mi) == m).sum()), 4)
        z = p[:, 2]
        self.assertAlmostEqual(float(z.max()) - 12.0, sebusstops.MOUNT_M + 0.205 + sebusstops.GAP_M + sebusstops.E22_M,
                               delta=0.08)
        y = p[:, 1] + self.box[1] - self.yc
        self.assertLess(float(y.max()), -3.5)                       # nothing over the carriageway
        self.assertGreater(float(y.min()), -3.5 - sebusstops.KERB_M - sebusstops.MAX_W_M)

    def test_stops_of_one_name_share_its_picture_and_an_unnamed_one_has_e22(self):
        mesh, faces, _ = self.tile([("a", self.x0, -5.0, "bus_stop", "Torget", {}),
                                    ("b", self.x0 + 30.0, 5.0, "bus_stop", "Torget", {}),
                                    ("c", self.x0 + 90.0, 5.0, "bus_stop", None, {})])
        materials, mi = mesh[3], np.asarray(mesh[4])
        self.assertEqual(len(faces), 2)
        self.assertEqual(int((mi == 1).sum()), 12)                  # E22: three poles, two faces of two triangles
        self.assertEqual(int((mi == 2).sum()), 8)

    def test_none_elsewhere_nor_for_another_tiles_stop(self):
        stop = [("a", self.x0, -5.0, "bus_stop", "Torget", {})]
        self.assertEqual(self.tile(stop, country="NO")[:2], (None, {}))
        self.assertEqual(self.tile(stop, tx=1)[:2], (None, {}))

    def test_the_sign_tile_asks_for_them_in_sweden(self):
        import inspect
        src = inspect.getsource(signstream.build_tile)
        self.assertIn("sebusstops.sign_tile", src)
        self.assertIn("sebusstops.wanted(country)", src)


if __name__ == "__main__":
    unittest.main()
