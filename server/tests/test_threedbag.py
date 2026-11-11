"""3DBAG (threedbag.py): roof faces to a building's measured roof; matching to the map's footprints; the map wins."""
import gzip
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import threedbag as T  # noqa: E402

X0, Y0 = 121000.0, 487000.0         # central Amsterdam in RD New


def face(bag, x, y, w, h, azimuth, slope, hmin, hmax, ground=0.5, nok=None, area=None):
    return {"type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]},
            "properties": {"identificatie": f"NL.IMBAG.Pand.{bag}", "b3_azimut": azimuth, "b3_hellingshoek": slope,
                           "b3_h_min": hmin, "b3_h_max": hmax, "b3_h_maaiveld": ground, "b3_h_nok": nok,
                           "b3_opp_grond": area, "oorspronkelijkbouwjaar": 1890}}


def gabled(bag="0363100012000001", x=X0, y=Y0):
    # a 10 x 8 m house, its two slopes facing north and south, eaves 7 m, ridge 11.5 m
    return [face(bag, x, y, 10, 4, 180.0, 45.0, 7.5, 12.0, nok=12.0, area=80.0),
            face(bag, x, y + 4, 10, 4, 0.0, 45.0, 7.5, 12.0, nok=12.0, area=80.0)]


class FacesTest(unittest.TestCase):
    def test_two_opposite_slopes_are_a_gable(self):
        (r,) = T.records_from_faces(gabled())
        self.assertEqual(r["roof_shape"], "gabled")
        self.assertAlmostEqual(r["height"], 11.5)
        self.assertAlmostEqual(r["roof_height"], 4.5)
        self.assertIn(round(r["roof_direction"]) % 180, (0,))       # (the main face faces north or south)
        self.assertEqual(r["year"], 1890)
        lat, lon = T.rd_to_wgs(X0 + 5, Y0 + 4)
        self.assertAlmostEqual(r["lat"], lat, places=6)
        self.assertAlmostEqual(r["lon"], lon, places=6)

    def test_four_ways_is_hipped_and_none_flat(self):
        b = "0363100012000002"
        hip = [face(b, X0, Y0, 12, 3, 180.0, 40.0, 6.0, 9.0, nok=9.0), face(b, X0, Y0 + 7, 12, 3, 0.0, 40.0, 6.0, 9.0, nok=9.0),
               face(b, X0, Y0 + 3, 3, 4, 270.0, 40.0, 6.0, 9.0, nok=9.0), face(b, X0 + 9, Y0 + 3, 3, 4, 90.0, 40.0, 6.0, 9.0, nok=9.0)]
        self.assertEqual(T.records_from_faces(hip)[0]["roof_shape"], "hipped")
        flat = [face("0363100012000003", X0, Y0, 20, 15, 0.0, 2.0, 14.0, 14.3)]
        r = T.records_from_faces(flat)[0]
        self.assertEqual((r["roof_shape"], r["roof_height"]), ("flat", 0.0))
        self.assertAlmostEqual(r["height"], 13.8)

    def test_a_lean_to_and_a_big_single_slope(self):
        lean = [face("0363100012000004", X0, Y0, 6, 4, 90.0, 20.0, 3.0, 4.5, nok=4.5)]
        self.assertEqual(T.records_from_faces(lean)[0]["roof_shape"], "skillion")
        big = [face("0363100012000005", X0, Y0, 60, 40, 0.0, 15.0, 4.0, 14.0, nok=14.0)]
        r = T.records_from_faces(big)[0]
        self.assertIsNone(r["roof_shape"], "a 2,400 m2 single slope: a complex roof's part, its shape predicted")
        self.assertLessEqual(r["roof_height"], 0.6 * r["height"])


class MatchTest(unittest.TestCase):
    def test_nearest_of_a_similar_area_and_the_bag_id(self):
        recs = T.records_from_faces(gabled() + gabled("0363100012000009", X0 + 30, Y0))
        m = T.Matcher(recs)
        lat, lon = T.rd_to_wgs(X0 + 6, Y0 + 3)
        self.assertEqual(m.match(lat, lon, 85.0)["bag"], "0363100012000001")
        self.assertIsNone(m.match(lat, lon, 400.0), "an area 5x the record's: not this building")
        far_lat, far_lon = T.rd_to_wgs(X0 + 100, Y0 + 100)
        self.assertIsNone(m.match(far_lat, far_lon, 80.0))
        self.assertEqual(m.match(far_lat, far_lon, 80.0, "NL.IMBAG.Pand.0363100012000009")["bag"], "0363100012000009")

    def test_the_map_wins(self):
        (r,) = T.records_from_faces(gabled())
        out = T.apply({"class": "house"}, r)
        self.assertEqual((out["roof_shape"], out["height"], out["roof_height"]), ("gabled", 11.5, 4.5))
        kept = T.apply({"class": "house", "height": 9.0, "roof_shape": "hipped"}, r)
        self.assertEqual((kept["height"], kept["roof_shape"]), (9.0, "hipped"))
        self.assertNotIn("roof_height", kept)


class PlacesTest(unittest.TestCase):
    def test_only_the_netherlands(self):
        self.assertTrue(T.squares_over(52.37, 4.88, 52.375, 4.89))
        self.assertEqual(T.squares_over(48.15, 11.46, 48.16, 11.47), [], "Munich: no request")

    def test_rd_round_trip(self):
        lat, lon = T.rd_to_wgs(X0, Y0)
        x, y = T.wgs_to_rd(lat, lon)
        self.assertLess(abs(x - X0) + abs(y - Y0), 2.0)

    def test_kept_squares_are_credited(self):
        with tempfile.TemporaryDirectory() as d:
            lat, lon = T.rd_to_wgs(X0 + 100, Y0 + 100)
            self.assertFalse(T.credited_near(d, lat, lon, 500))
            (ix, iy), = T.squares_over(lat, lon, lat, lon)
            os.makedirs(os.path.join(d, "3dbag", T.VERSION))
            with gzip.open(os.path.join(d, "3dbag", T.VERSION, f"{ix}_{iy}.json.gz"), "wt") as fh:
                json.dump(T.records_from_faces(gabled()), fh)
            self.assertTrue(T.credited_near(d, lat, lon, 500))

    def test_a_failed_square_is_not_kept(self):
        class Down:
            def get(self, url, kind="demand"):
                raise IOError("offline")
        with tempfile.TemporaryDirectory() as d:
            src = T.Source(d, Down())
            self.assertIsNone(src.square((484, 1948)))
            self.assertFalse(os.path.exists(os.path.join(d, "3dbag", T.VERSION, "484_1948.json.gz")))


if __name__ == "__main__":
    unittest.main()
