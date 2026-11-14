"""navgraph.country_at: the road rules' country (mph, driving side, markings, lamps) from real borders where the server
has them (its regional scenery on), the nearest airport's elsewhere and without them (GitHub #1046: in British
Columbia's Lower Mainland the nearest airport can be in Washington, so the speed signs read mph on a Vienna-style
disc). Kehl's nearest airport is Strasbourg's, 5 km west across the Rhine: upstream's border fixture
(tests/fixtures/regional) calls it Germany."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "tests"))
import borderfixtures  # noqa: E402
import countrybounds  # noqa: E402
import earthd  # noqa: E402
import germany  # noqa: E402
import navgraph  # noqa: E402

KEHL, STRASBOURG = (48.573, 7.816), (48.583, 7.746)
SURREY_BC, BELLINGHAM = (49.005, -122.75), (48.793, -122.538)     # (a Lower Mainland town by the border, KBLI)


def setUpModule():
    borderfixtures.require()


def airports(*rows):
    """An airports.AirportDB stand-in: one runway per (ident, (lat, lon), country, region)."""
    runways = [SimpleNamespace(airport_ident=ident, country=None) for ident, _, _, _ in rows]
    return SimpleNamespace(runways=runways, _mid=np.array([at for _, at, _, _ in rows], np.float64),
                           airports={ident: SimpleNamespace(country=c, region=r) for ident, _, c, r in rows})


class Index:
    """A border index that answers code everywhere."""

    def __init__(self, code):
        self.code = code

    def at(self, lat, lon):
        return self.code


class CountryAtTest(unittest.TestCase):

    def setUp(self):
        germany._failed[:] = [-float("inf"), 0]
        navgraph._NEAREST.clear()

    def test_a_border_town_takes_its_border_not_the_airport_across_it(self):
        db = airports(("LFST", STRASBOURG, "FR", "FR-GES"))
        self.assertEqual(navgraph.country_near(db, *KEHL), "FR", "the stand-in's error this fixes")
        self.assertEqual(navgraph.country_at(db, *KEHL, borders=True), "DE")
        self.assertEqual(navgraph.country_at(db, *STRASBOURG, borders=True), "FR")
        self.assertIsNone(navgraph.region_at(db, *KEHL, borders=True), "Grand Est's rules stay in France")
        self.assertEqual(navgraph.region_at(db, *STRASBOURG, borders=True), "FR-GES")

    def test_without_borders_the_airport_as_before_and_no_border_asked(self):
        db = airports(("LFST", STRASBOURG, "FR", "FR-GES"))
        with mock.patch.object(countrybounds, "bundled", side_effect=AssertionError("border data read")):
            self.assertEqual(navgraph.country_at(db, *KEHL), "FR")
            self.assertEqual(navgraph.region_at(db, *KEHL), "FR-GES")

    def test_no_border_hit_or_no_data_falls_back_to_the_airport(self):
        db = airports(("KBLI", BELLINGHAM, "US", "US-WA"))
        for idx in (Index(None), countrybounds.Absent()):
            with mock.patch.object(countrybounds, "bundled", return_value=idx):
                self.assertEqual(navgraph.country_at(db, *SURREY_BC, borders=True), "US")
                self.assertEqual(navgraph.region_at(db, *SURREY_BC, borders=True), "US-WA")

    def test_a_lookup_that_raises_falls_back_and_backs_off(self):
        db = airports(("KBLI", BELLINGHAM, "US", "US-WA"))
        with mock.patch.object(countrybounds, "bundled", side_effect=OSError("disk full")), \
                self.assertLogs("germany", "WARNING"):
            self.assertEqual(navgraph.country_at(db, *SURREY_BC, borders=True), "US")
        with mock.patch.object(countrybounds, "bundled", side_effect=AssertionError("asked during the back-off")):
            self.assertEqual(navgraph.country_at(db, *SURREY_BC, borders=True), "US")

    def test_british_columbia_by_a_us_airport_posts_kmh(self):
        """GitHub #1046's case through the server's own rules: the anchor's mph, the geotile's and the sign style."""
        import geotiles
        import speedsigns
        db = airports(("KBLI", BELLINGHAM, "US", "US-WA"))
        with mock.patch.object(countrybounds, "bundled", return_value=Index("ca")):
            for poles, mph in (("europe", False), ("off", True)):
                server = SimpleNamespace(airports_db=db, regional_poles=poles)
                anchor = SimpleNamespace(lat=SURREY_BC[0], lon=SURREY_BC[1])
                self.assertEqual(earthd.Server.mph(server, anchor), mph, poles)
                gx, gy = geotiles.tile_of(*SURREY_BC)[:2]
                self.assertEqual(earthd.Server.geo_rules(server, gx, gy)[1], mph, poles)
                country = earthd.Server.country(server, SimpleNamespace(lat=SURREY_BC[0], lon=SURREY_BC[1]))
                self.assertEqual(speedsigns.style(country, us=False),
                                 speedsigns.STYLE_CANADA if poles != "off" else speedsigns.STYLE_VIENNA, poles)


if __name__ == "__main__":
    unittest.main()
