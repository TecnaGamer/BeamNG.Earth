"""Pin flags (golf.py): a golf=pin point, and the end of a golf=hole way."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import golf
import mapgeom


class FlagTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(39.28, -76.61, 0.0)

    def feats(self, items):
        """items: (id, kind, rings of [x, y], class, tags)."""
        ids, kinds, parts = [], [], []
        classes, tags = [], []
        for fid, kind, rings, cls, tag in items:
            ids.append(fid)
            kinds.append(kind)
            parts.append(rings)
            classes.append(cls)
            tags.append(json.dumps(tag or {}))
        lonlat, part_off, ring_off, coord_off = [], [0], [0], [0]
        for rings in parts:
            part_off.append(part_off[-1] + len(rings))
            for ring in rings:
                xy = np.asarray(ring, np.float64)
                if len(xy) == 0:
                    lonlat.append(np.zeros((0, 2)))
                else:
                    lat, lon, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
                    lonlat.append(np.column_stack((lon, lat)))
                ring_off.append(ring_off[-1] + 1)
                coord_off.append(coord_off[-1] + len(ring))
        return mapgeom.Features(
            layer="land_use", ids=np.array(ids), kind=np.array(kinds, np.uint8),
            part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
            coord_off=np.array(coord_off, np.int64),
            lonlat=np.vstack(lonlat) if lonlat else np.zeros((0, 2)),
            attrs={"class": np.array(classes), "subtype": np.array(["golf"] * len(ids)),
                   "tags": np.array(tags)})

    def test_a_pin_and_a_hole_end_and_not_the_green(self):
        land = self.feats([
            ("pin-1", mapgeom.POINT, [[[10.0, 20.0]]], "green", {"golf": "pin"}),
            ("hole-1", mapgeom.LINE, [[[0.0, 0.0], [40.0, 5.0], [80.0, 12.0]]], "fairway", {"golf": "hole"}),
            ("hole-near", mapgeom.LINE, [[[0.0, 0.0], [12.0, 21.0]]], "", {"golf": "hole"}),
            ("green-1", mapgeom.POLYGON, [[[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [0.0, 30.0], [0.0, 0.0]]],
             "green", {"golf": "green"}),
            ("away", mapgeom.POINT, [[[500.0, 500.0]]], "", {"golf": "pin"}),
        ])
        rows = golf.rows(self.frame, lambda key: land, (0.0, 0.0, 200.0, 200.0), lambda la, lo: np.zeros(len(la)))
        spots = {(round(r[1], 1), round(r[2], 1)) for r in rows}
        self.assertEqual(len(rows), 2)
        self.assertIn((10.0, 20.0), spots)
        self.assertIn((80.0, 12.0), spots)
        self.assertTrue(all(r[0] == golf.G_FLAG and r[6] == golf.FLAG_H for r in rows))
        again = golf.rows(self.frame, lambda key: land, (0.0, 0.0, 200.0, 200.0), lambda la, lo: np.full(len(la), 3.0))
        self.assertEqual([r[3] for r in again], [3.0, 3.0])

    def test_the_same_pin_from_two_cells_is_one_flag(self):
        one = self.feats([("pin-1", mapgeom.POINT, [[[4.0, 6.0]]], "", {"golf": "pin"})])
        two = self.feats([("pin-1", mapgeom.POINT, [[[4.0, 6.0]]], "", {"golf": "pin"})])
        calls = {"n": 0}

        def land(key):
            calls["n"] += 1
            return one if calls["n"] == 1 else two
        rows = golf.rows(self.frame, land, (0.0, 0.0, 50.0, 50.0), lambda la, lo: np.zeros(len(la)))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
