"""Sweden's reading of a road (roads._swedish_specs, sweden.py): its kind and width where the map says little."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import roads  # noqa: E402
import sweden  # noqa: E402
import test_roads as tr  # noqa: E402

LINE = [[(0, 0), (500, 0)]]
TOWN = staticmethod(lambda la, lo: np.ones(np.shape(la), bool))
COUNTRY = staticmethod(lambda la, lo: np.zeros(np.shape(la), bool))


def spec(cls, urban=False, attrs=None, **kw):
    lat, lon = kw.pop("at", (57.72, 12.94))
    with mock.patch.multiple(tr, LAT=lat, LON=lon, M_LON=111320.0 * np.cos(np.radians(lat))):
        f = tr.features(LINE, [cls], **kw)
    for k, v in (attrs or {}).items():
        f.attrs[k] = np.array([v], object)
    fn = (lambda la, lo: np.ones(np.shape(la), bool)) if urban else None
    return roads.road_specs(f, fn)[0]


class OutlineTest(unittest.TestCase):
    def test_places(self):
        inside = {"Malmö": (55.6, 13.0), "Visby": (57.64, 18.3), "Kiruna": (67.85, 20.25), "Göteborg": (57.7, 11.97),
                  "Umeå": (63.83, 20.26), "Haparanda": (65.83, 24.1), "Stockholm": (59.33, 18.07), "Östersund": (63.18, 14.64)}
        outside = {"Oslo": (59.91, 10.75), "Köpenhamn": (55.68, 12.57), "Helsingfors": (60.17, 24.94),
                   "Tromsø": (69.65, 18.96), "Trondheim": (63.43, 10.4), "Mariehamn": (60.1, 19.93),
                   "Rønne": (55.1, 14.7), "Boston": (42.36, -71.06)}
        for name, (la, lo) in inside.items():
            self.assertTrue(sweden.contains(lo, la)[0], name)
        for name, (la, lo) in outside.items():
            self.assertFalse(sweden.contains(lo, la)[0], name)


class KindTest(unittest.TestCase):
    def test_rural_minor_roads_with_no_surface_are_gravel(self):
        for cls in ("unclassified", "service", "unknown"):
            self.assertEqual(spec(cls).kind, "gravel", cls)
        self.assertEqual(spec("tertiary").kind, "paved")
        self.assertEqual(spec("residential").kind, "paved")

    def test_in_town_they_stay_paved(self):
        for cls in ("unclassified", "service", "unknown"):
            self.assertEqual(spec(cls, urban=True).kind, "paved", cls)

    def test_a_mapped_surface_stands(self):
        self.assertEqual(spec("unclassified", surfaces=[(0, "paved")]).kind, "paved")
        self.assertEqual(spec("unclassified", attrs={"osm_surface": "asphalt"}).kind, "paved")
        self.assertEqual(spec("tertiary", attrs={"osm_surface": "gravel"}).kind, "gravel")

    def test_elsewhere_nothing_changes(self):
        got = spec("unclassified", at=(tr.LAT, tr.LON))
        self.assertEqual(got.kind, "paved")
        self.assertTrue(got.parking)
        self.assertEqual(spec("residential", urban=True, at=(tr.LAT, tr.LON)).width,
                         roads.default_width("residential", "paved", True, region="US"))

    def test_a_fast_road_is_no_town_street(self):
        self.assertTrue(spec("primary", urban=True, attrs={"osm_maxspeed": 50.0}).urban)
        self.assertFalse(spec("primary", urban=True, attrs={"osm_maxspeed": 70.0}).urban)


class WidthTest(unittest.TestCase):
    def test_one_width_in_town_and_out(self):
        for cls in ("residential", "tertiary", "secondary", "primary", "trunk", "unclassified"):
            self.assertEqual(spec(cls, urban=True, surfaces=[(0, "paved")]).width, spec(cls, surfaces=[(0, "paved")]).width, cls)

    def test_no_parking_lanes(self):
        got = spec("residential", urban=True)
        self.assertEqual(got.width, roads.STYLE["residential"][0])
        self.assertEqual(roads.lane_layout(got)[0], got.width, "all of it is driven")

    def test_a_mapped_width_stands(self):
        self.assertEqual(spec("residential", urban=True, widths=[(0, 9.0)]).width, 9.0)


class LaneFitTest(unittest.TestCase):
    def test_the_ai_gets_the_lanes_the_road_has_room_for(self):
        sp = roads.Spec(4.5, "paved", True, "residential", True, lanes=2, parking=False)
        self.assertEqual(roads.lane_layout(sp)[1], 1)
        self.assertEqual(roads.lane_layout(sp._replace(width=8.0))[1], 2)


if __name__ == "__main__":
    unittest.main()
