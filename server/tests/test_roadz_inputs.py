"""Bad survey vertices must not discard a cell or invent a road across a gap."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roadz
import mapcells


def ign(coords, level=1, multi=False):
    return {"properties": {"position_par_rapport_au_sol": level, "cpx_numero": "A75"},
            "geometry": {"type": "MultiLineString" if multi else "LineString", "coordinates": coords}}


def nvdb(wkt, medium="L"):
    return {"geometri": {"wkt": wkt, "medium": medium}}


class SurveyInputTest(unittest.TestCase):
    def test_ign_bad_vertex_does_not_discard_other_roads(self):
        good = [[3.0, 44.0, 10], [3.001, 44.001, 11]]
        for bad in (None, [], [3], ["bad", 44, 1], [float("nan"), 44, 1],
                    [3, float("inf"), 1], [181, 44, 1], [3, 91, 1]):
            with self.subTest(bad=bad):
                rows = roadz.parse_ign([ign([bad, good[0]]), ign(good, -1)])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][2], -1, "the unrelated tunnel keeps its level")
                np.testing.assert_array_equal(rows[0][0], np.array(good)[:, :2])

    def test_ign_invalid_vertex_splits_without_inventing_connection(self):
        a = [[3.0, 44, 10], [3.001, 44, 11]]
        b = [[3.1, 44, 12], [3.101, 44, 13]]
        rows = roadz.parse_ign([ign(a + [None] + b)])
        self.assertEqual(len(rows), 2)
        np.testing.assert_array_equal(rows[0][0], np.array(a)[:, :2])
        np.testing.assert_array_equal(rows[1][0], np.array(b)[:, :2])
        self.assertEqual([r[2] for r in rows], [1, 1])

    def test_ign_bad_height_remains_missing_on_valid_geometry(self):
        for z in (None, "bad", float("inf"), float("-inf"), float("nan"), -1000):
            with self.subTest(z=z):
                rows = roadz.parse_ign([ign([[3, 44, z], [3.001, 44, 7]])])
                self.assertEqual(len(rows), 1)
                self.assertTrue(np.isnan(rows[0][1][0]))
                self.assertEqual(rows[0][1][1], 7)

    def test_ign_multiline_keeps_valid_components(self):
        rows = roadz.parse_ign([ign([None, [[3, 44, 8], [3.01, 44, 9]], []], multi=True)])
        self.assertEqual(len(rows), 1)
        np.testing.assert_array_equal(rows[0][1], [8, 9])

    def test_ign_bad_feature_shape_does_not_discard_cell(self):
        rows = roadz.parse_ign([None, [], "bad", {"properties": []}, {"geometry": "bad"},
                               ign([[3, 44, 8], [3.01, 44, 9]])])
        self.assertEqual(len(rows), 1)

    def test_nvdb_bad_vertex_splits_without_inventing_connection(self):
        for bad in ("invalid", "59.9 bad 1", "nan 10.7 1", "91 10.7 1", "59.9 181 1"):
            with self.subTest(bad=bad):
                rows = roadz.parse_nvdb([nvdb("LINESTRING Z (59.9 10.7 8, 59.901 10.7 9, " + bad +
                                             ", 59.95 10.7 10, 59.951 10.7 11)", "U")])
                self.assertEqual(len(rows), 2)
                self.assertEqual([r[2] for r in rows], [-1, -1])
                np.testing.assert_array_equal(rows[0][1], [8, 9])
                np.testing.assert_array_equal(rows[1][1], [10, 11])

    def test_nvdb_missing_height_remains_missing(self):
        for z in ("bad", "inf", "-inf", "nan"):
            with self.subTest(z=z):
                rows = roadz.parse_nvdb([nvdb(f"LINESTRING Z (59.9 10.7 {z}, 59.901 10.7 9)")])
                self.assertEqual(len(rows), 1)
                self.assertTrue(np.isnan(rows[0][1][0]))

    def test_nvdb_bad_object_shape_does_not_discard_cell(self):
        rows = roadz.parse_nvdb([None, [], "bad", {"geometri": "bad"}, {"geometri": {"wkt": 4}},
                                nvdb("LINESTRING Z (59.9 10.7 8, 59.901 10.7 9)")])
        self.assertEqual(len(rows), 1)

    def test_nvdb_rejects_other_geometry_instead_of_partial_match(self):
        self.assertEqual(roadz.parse_nvdb([nvdb("MULTILINESTRING Z ((59.9 10.7 8, 59.901 10.7 9))")]), [])

    def test_valid_unsmoothed_grades_are_preserved(self):
        heights = [10.0, 12.0, 11.0, 15.0]
        for level in (-1, 0, 1):
            rows = roadz.parse_ign([ign([[3 + i * .001, 44, z] for i, z in enumerate(heights)], level)])
            self.assertEqual(rows[0][2], level)
            np.testing.assert_array_equal(rows[0][1], heights)

    def test_invalid_cached_geometry_is_refetched_and_repaired(self):
        key = mapcells.CellKey(14, 8300, 5900)
        with tempfile.TemporaryDirectory() as cache:
            provider = roadz.RoadZ(cache, object())
            path = provider._path("ign", key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            np.savez_compressed(path, lonlat=[[3, 44], [float("nan"), 44]], z=[8, 9],
                                off=[0, 2], level=[1], ref=["A75"], name=[""], corrected=False)
            calls = []

            def fetch(*args, **kwargs):
                calls.append(1)
                return {"features": [ign([[3, 44, 8], [3.001, 44, 9]])]}

            provider._get_json = fetch
            got = provider.lines("ign", key)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(got), 1)
            self.assertTrue(np.isfinite(got.lonlat).all())
            self.assertTrue(np.isfinite(roadz.Lines.load(path).lonlat).all(), "cache repaired on disk")
            self.assertIs(provider.lines("ign", key), got)
            self.assertEqual(len(calls), 1, "memory hit needs no refetch")

    def test_cached_infinite_height_is_missing(self):
        lines = roadz.Lines([[3, 44], [3.001, 44]], [float("inf"), 9], [0, 2], [1], [""], [""])
        self.assertTrue(np.isnan(lines.z[0]))
        self.assertEqual(lines.z[1], 9)


if __name__ == "__main__":
    unittest.main()
