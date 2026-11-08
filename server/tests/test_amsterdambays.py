"""Amsterdam's marked bays: the E-code, a bay outside the tile, an empty reply."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import amsterdambays as bays  # noqa: E402


def _feat(ring, e_type):
    return {"type": "Feature", "properties": {"e_type": e_type},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


class KindTest(unittest.TestCase):
    def test_taxi_code_stays_a_taxi(self):
        self.assertEqual(bays.kind_of("E8"), "taxi")
        self.assertEqual(bays.kind_of("E6b"), "disabled")
        self.assertEqual(bays.kind_of("E7"), "loading")
        self.assertEqual(bays.kind_of(None), "ordinary")


class SpotTest(unittest.TestCase):
    def test_a_bay_outside_the_tile_is_dropped(self):
        # RD of a point that rd_to_wgs puts near the origin of this projector, and one far away.
        near = _feat([(155000, 463000), (155006, 463000), (155006, 463002), (155000, 463002), (155000, 463000)], "E8")
        far = _feat([(200000, 500000), (200006, 500000), (200006, 500002), (200000, 500002), (200000, 500000)], "E7")

        def project(lat, lon):
            return (lon - 5.38720621) * 1e5, (lat - 52.15517440) * 1e5

        rows = bays.spots_from_features([near, far], project, (0, 0, 50, 50))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "taxi")

    def test_an_empty_response_leaves_the_inferred_spots(self):
        inferred = [[1.0, 2.0, 0.0, 90.0], [8.0, 2.0, 0.0, 90.0]]
        self.assertEqual(bays.replace_overlapping(inferred, []), inferred)
        self.assertEqual(bays.for_tile(None, (0, 0, 1, 1), fetch=lambda url: b""), [])

    def test_a_surveyed_bay_replaces_the_inferred_spot_on_it(self):
        inferred = [[1.0, 2.0, 0.0, 0.0], [30.0, 2.0, 0.0, 0.0]]
        surveyed = [[1.2, 2.1, 0.0, 10.0, "taxi"]]
        rows = bays.replace_overlapping(inferred, surveyed)
        self.assertEqual(rows[0][:2], [30.0, 2.0])
        self.assertEqual(rows[1][4], "taxi")
