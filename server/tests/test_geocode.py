"""geocode: the map's place search: its sources, their ranking, and what the map is told."""
import json
import sys
import time
import types
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geocode  # noqa: E402


class FakeNet:
    """Captures the requested URLs (Photon's come first); answers with an empty Photon-shaped response."""
    def __init__(self):
        self.urls = []

    def get(self, url, kind="demand", lane=None):
        self.urls.append(url)
        self.lane = lane
        return types.SimpleNamespace(body=b'{"features": []}')

    def photon(self):
        return next(u for u in self.urls if "photon" in u)


class LangForTest(unittest.TestCase):
    def setUp(self):
        geocode._cache.clear()
        geocode._skip.clear()

    def test_by_country(self):
        for cc in ("DE", "AT", "CH", "li"):
            self.assertEqual(geocode.lang_for(cc), "de", cc)
        self.assertEqual(geocode.lang_for("FR"), "fr")
        self.assertEqual(geocode.lang_for("IT"), "it")
        for cc in ("US", "CZ", "PL", "NL", "SI", None, ""):
            self.assertEqual(geocode.lang_for(cc), "en", cc)

    def test_search_picks_it_up(self):
        net = FakeNet()
        geocode.search("Koeln", 50.9375, 6.9603, net=net, country="DE")
        self.assertEqual(parse_qs(urlparse(net.photon()).query)["lang"], ["de"])
        net = FakeNet()
        geocode.search("Praha", 50.08, 14.42, net=net, country="CZ")      # (inside the PR's DACH box)
        self.assertEqual(parse_qs(urlparse(net.photon()).query)["lang"], ["en"])
        net = FakeNet()
        geocode.search("fells point", 39.2904, -76.6122, net=net)
        self.assertEqual(parse_qs(urlparse(net.photon()).query)["lang"], ["en"])
        net = FakeNet()
        geocode.search("Zermatt", 46.0207, 7.7491, net=net, country="CH")
        self.assertEqual(parse_qs(urlparse(net.photon()).query)["lang"], ["de"])


class GeocodeTest(unittest.TestCase):
    def test_url_biased_to_the_player(self):
        q = parse_qs(urlparse(geocode.url(" fells point ", 39.28321, -76.59311)).query)
        self.assertEqual((q["q"], q["lat"], q["lon"]), (["fells point"], ["39.2832"], ["-76.5931"]))
        self.assertNotIn("lat", parse_qs(urlparse(geocode.url("x y z")).query))

    def test_parse_labels(self):
        data = {"features": [
            {"geometry": {"coordinates": [-76.5932, 39.2835]},
             "properties": {"name": "Fell's Point", "city": "Baltimore", "state": "Maryland", "country": "United States",
                            "osm_value": "neighbourhood"}},
            {"geometry": {"coordinates": [-77.0366, 38.8977]},
             "properties": {"housenumber": "1600", "street": "Pennsylvania Avenue Northwest", "city": "Washington"}},
            {"geometry": {"coordinates": [999, 0]}, "properties": {"name": "bad"}},
            {"properties": {"name": "no geometry"}}]}
        r = geocode.parse(data)
        self.assertEqual(len(r), 2)
        self.assertEqual((r[0]["name"], r[0]["detail"], r[0]["kind"]),
                         ("Fell's Point", "Baltimore, Maryland, United States", "neighbourhood"))
        self.assertEqual(r[1]["name"], "1600 Pennsylvania Avenue Northwest")
        self.assertEqual(r[1]["detail"], "Washington")

    def test_short_query_is_not_sent(self):
        class Boom:
            def get(self, *a, **k):
                raise AssertionError("no request for a short query")
        self.assertEqual(geocode.search("ab", net=Boom()), [])


class ErrorTextTest(unittest.TestCase):
    """GitHub #602: photon.komoot.io answered 403 to the app's User-Agent and the map said "Address search unavailable
    (offline?)" for it, as for every failure: the player was sent to check a connection that worked."""

    def setUp(self):
        geocode._refused.clear()

    def test_each_failure_says_what_it_is(self):
        import netfetch as nf
        self.assertEqual(geocode.error_text(nf.FetchFailed("x", status=403), now=0.0),
                         "Address search service refused the request (HTTP 403)")
        self.assertEqual(geocode.error_text(nf.FetchFailed("x", status=500), now=0.0),
                         "Address search service error (HTTP 500)")
        self.assertIn("busy", geocode.error_text(nf.RateLimited("x", status=429), now=0.0))
        self.assertIn("offline mode", geocode.error_text(nf.Offline("x"), now=0.0))
        self.assertIn("limit", geocode.error_text(nf.BudgetExceeded("x"), now=0.0))
        self.assertEqual(geocode.error_text(nf.FetchFailed("timed out"), now=0.0),
                         "Address search unavailable (offline?)", "no status: a network error, maybe offline")
        self.assertIn("cannot read", geocode.error_text(ValueError("bad json"), now=0.0))

    def test_a_refusal_is_still_named_once_the_breaker_opens(self):
        import netfetch as nf
        self.assertIn("not answering", geocode.error_text(nf.HostUnavailable("open"), now=0.0))
        geocode.error_text(nf.FetchFailed("x", status=403), now=10.0)
        self.assertIn("refused the request (HTTP 403)", geocode.error_text(nf.HostUnavailable("open"), now=20.0))
        self.assertIn("not answering", geocode.error_text(nf.HostUnavailable("open"),
                                                          now=10.0 + geocode.REFUSED_KEEP_S + 1))

    def test_through_netfetch(self):
        """The real Fetcher on a host answering 403 (photon's answer to the app): only Photon is asked for a while."""
        import netfetch as nf
        from test_netfetch import fetcher
        geocode._cache.clear()
        geocode._skip.clear()
        f, clock, sleep, tr = fetcher({r".*": [(403, {}, b"Forbidden", 0.0)]})
        said = []
        for q in ("Selfoss", "Reykjavik", "Akureyri"):
            try:
                geocode.search(q, 63.9, -21.0, net=f)
                said.append(None)
            except nf.FetchError as e:
                said.append(geocode.error_text(e, now=clock()))
        self.assertTrue(all(t == "Address search service refused the request (HTTP 403)" for t in said), said)


class Answers:
    """A net that answers by the URL's host: {host part: bytes or an exception}. Anything else: an empty search."""
    def __init__(self, table):
        self.table = table
        self.urls = []

    def get(self, url, kind="demand", lane=None):
        self.urls.append(url)
        for part, answer in self.table.items():
            if part in url:
                if isinstance(answer, Exception):
                    raise answer
                return types.SimpleNamespace(body=answer)
        return types.SimpleNamespace(body=b"{}")


def wikipedia_answer(*titles):
    pages = [{"title": t, "index": i, "description": "a place", "coordinates": [{"lat": 46.0 + i, "lon": 8.0, "globe": "earth"}]}
             for i, t in enumerate(titles, 1)]
    return json.dumps({"query": {"pages": pages}}).encode()


class SourcesTest(unittest.TestCase):
    def setUp(self):
        geocode._cache.clear()
        geocode._skip.clear()

    def test_wikipedia_keeps_only_places_on_earth(self):
        r = geocode.parse_wikipedia({"query": {"pages": [
            {"title": "List of bridges", "index": 1},
            {"title": "Mons Huygens", "index": 2, "coordinates": [{"lat": 1, "lon": 2, "globe": "moon"}]},
            {"title": "Ganter Bridge", "index": 3, "description": "bridge", "coordinates": [{"lat": 46.3, "lon": 8.05}]}]}})
        self.assertEqual([x["name"] for x in r], ["Ganter Bridge"])

    def test_wikidata_in_the_search_order(self):
        def item(label, globe="http://www.wikidata.org/entity/Q2"):
            return {"labels": {"en": {"value": label}}, "claims": {"P625": [{"mainsnak": {"datavalue": {"value": {
                "latitude": 1.0, "longitude": 2.0, "globe": globe}}}}]}}
        data = {"entities": {"Q1": item("b"), "Q2": item("a"), "Q3": item("moon", "x/Q405"), "Q4": {"labels": {}}}}
        self.assertEqual([x["name"] for x in geocode.parse_wikidata(data, ["Q2", "Q1", "Q3", "Q4"], "de")], ["a", "b"])

    def test_nominatim_labels(self):
        r = geocode.parse_nominatim([{"lat": "51.0", "lon": "9.2", "name": "", "type": "house", "display_name": "x",
                                      "address": {"road": "Lindenweg", "house_number": "6", "village": "Altdorf",
                                                  "state": "Hessen", "country": "Deutschland"}}, {"lat": "bad"}])
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["name"], r[0]["detail"]), ("Lindenweg 6", "Altdorf, Hessen, Deutschland"))

    def test_nominatim_only_on_enter(self):
        """Its usage policy forbids autocomplete."""
        net = Answers({})
        geocode.search("Lindenweg", net=net)
        self.assertFalse([u for u in net.urls if "nominatim" in u])
        net = Answers({})
        geocode.search("Lindenweg", net=net, full=True)
        self.assertTrue([u for u in net.urls if "nominatim" in u])

    def test_a_refused_photon_is_left_alone_and_the_others_answer(self):
        import netfetch as nf
        net = Answers({"photon": nf.FetchFailed("x", status=403), "wikipedia": wikipedia_answer("Ganter Bridge")})
        self.assertEqual(geocode.search("Ganter Bridge", net=net)[0]["name"], "Ganter Bridge")
        geocode._cache.clear()
        net.urls.clear()
        geocode.search("Ganter Bridge", net=net)
        self.assertFalse([u for u in net.urls if "photon" in u])

    def test_an_error_only_when_every_source_failed(self):
        import netfetch as nf
        net = Answers({"http": nf.FetchFailed("timed out")})
        with self.assertRaises(nf.FetchError):
            geocode.search("Ganter Bridge", net=net)

    def test_nothing_found_is_not_an_error(self):
        import netfetch as nf
        net = Answers({"photon": nf.FetchFailed("x", status=403)})
        self.assertEqual(geocode.search("xqzzy", net=net), [])

    def test_a_slow_source_does_not_hold_the_rest(self):
        class Slow(Answers):
            def get(self, url, kind="demand", lane=None):
                if "photon" in url:
                    time.sleep(0.5)
                return super().get(url, kind, lane)
        with mock.patch.object(geocode, "TYPING_DEADLINE_S", 0.1):
            net = Slow({"wikipedia": wikipedia_answer("Ganter Bridge")})
            t = time.monotonic()
            self.assertTrue(geocode.search("Ganter Bridge", net=net))
            self.assertLess(time.monotonic() - t, 0.4)

    def test_a_word_is_asked_once_an_hour(self):
        net = Answers({"wikipedia": wikipedia_answer("Ganter Bridge")})
        geocode.search("Ganter Bridge", net=net)
        n = len(net.urls)
        geocode.search("Ganter Bridge", net=net)
        self.assertEqual(len(net.urls), n)

    def test_two_words_are_also_asked_as_one_and_alone(self):
        self.assertEqual(geocode.variants("Felsenau Viadukt"), ["Felsenauviadukt", "Felsenau", "Viadukt"])
        self.assertEqual(geocode.variants("Ganterbrücke"), [])

    def test_names_in_the_players_language_abroad(self):
        net = Answers({})
        with mock.patch.object(geocode, "user_lang", return_value="de"):
            geocode.search("Lindenweg Altdorf", -9.3, -77.6, net=net, country="PE", full=True)
        self.assertTrue(all("accept-language=de" in u for u in net.urls if "nominatim" in u))

    def test_languages(self):
        self.assertEqual(geocode.wiki_langs("CH", "de"), ["de", "en"])
        self.assertEqual(geocode.wiki_langs("JP", "de"), ["ja", "de", "en"])
        self.assertEqual(geocode.wiki_langs(None, ""), ["en"])
        self.assertEqual(geocode.wiki_langs("US", "xx"), ["en"])


class RankingTest(unittest.TestCase):
    def test_fold(self):
        for a, b in (("Ganterbrücke", "Ganterbruecke"), ("Straße", "STRASSE"), ("Transfăgărășan", "Transfagarasan"),
                     ("Hauptstr.", "Hauptstrasse")):
            self.assertEqual(geocode.fold(a), geocode.fold(b))

    def test_the_named_place_beats_the_near_one(self):
        far = {"name": "Ganterbrücke", "detail": "", "lat": 46.3, "lon": 8.05, "rank": 5}
        near = {"name": "Gander Bridge", "detail": "", "lat": 38.6, "lon": -83.3, "rank": 1}
        self.assertEqual(geocode.merge("Ganterbruecke", [near, far], 38.6, -83.3)[0]["name"], "Ganterbrücke")

    def test_the_longer_word_decides(self):
        pass_ = {"name": "Simplonpass", "detail": "", "lat": 46.25, "lon": 8.03, "rank": 1}
        bridge = {"name": "Ganterbrücke", "detail": "", "lat": 46.3, "lon": 8.05, "rank": 2}
        self.assertEqual(geocode.merge("Ganterbrücke Simplon", [pass_, bridge])[0]["name"], "Ganterbrücke")

    def test_one_line_per_place(self):
        same = [{"name": "Sitterviadukt", "detail": d, "lat": 47.4, "lon": 9.3 + i * 1e-3, "rank": 1}
                for i, d in enumerate(("", "viaduct", "x"))]
        r = geocode.merge("Sitterviadukt", same)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["detail"], "viaduct")

    def test_one_line_for_a_place_in_two_languages(self):
        for names in (("Umbrailpass", "Umbrail Pass"), ("Ganter Bridge", "Ganterbrücke"), ("Eiffel Tower", "Eiffelturm")):
            r = [{"name": n, "detail": "", "lat": 46.5, "lon": 10.4 + i * 1e-3} for i, n in enumerate(names)]
            self.assertEqual(len(geocode.merge(names[0], r)), 1, names)

    def test_two_articles_on_one_large_place(self):
        r = [{"name": "Kanton Bern", "detail": "", "kind": k, "lat": 46.8 + i * 0.1, "lon": 7.6} for i, k in
             enumerate(("wikipedia", "wikidata"))]
        self.assertEqual(len(geocode.merge("Kanton Bern", r)), 1)

    def test_two_places_side_by_side_stay(self):
        r = [{"name": "Nürburgring", "detail": "", "lat": 50.33, "lon": 6.94},
             {"name": "Nordschleife", "detail": "", "lat": 50.332, "lon": 6.94}]
        self.assertEqual(len(geocode.merge("Nürburgring Nordschleife", r)), 2)

    def test_a_same_name_far_away_stays(self):
        a = {"name": "Hauptstraße", "detail": "Kassel", "lat": 51.3, "lon": 9.5}
        b = {"name": "Hauptstraße", "detail": "Bern", "lat": 46.9, "lon": 7.4}
        self.assertEqual(len(geocode.merge("Hauptstraße", [a, b])), 2)

    def test_weak_lines_go_when_a_good_one_is_there(self):
        good = {"name": "Stelvio Pass", "detail": "", "lat": 46.5, "lon": 10.4}
        junk = {"name": "Corvara", "detail": "", "lat": 46.5, "lon": 11.8}
        self.assertEqual([r["name"] for r in geocode.merge("Stelvio Pass", [junk, good])], ["Stelvio Pass"])
        self.assertEqual(geocode.merge("Musterweg", [junk]), [], "an article that only mentions the word")
        near = {"name": "Stelvio Hotel", "detail": "", "lat": 46.5, "lon": 10.4}
        self.assertTrue(geocode.merge("Stelvio Passo", [near]), "nothing better: the weak line stays")

    def test_an_address_goes_by_its_name_not_its_town(self):
        a = {"name": "Wetzikon ZH", "detail": "Bahnhofstrasse 1, Zürich", "kind": "station", "lat": 47.3, "lon": 8.8, "rank": 1}
        b = {"name": "Bahnhofstrasse 1", "detail": "Zürich", "kind": "house", "lat": 47.4, "lon": 8.5, "rank": 2}
        self.assertEqual(geocode.merge("Bahnhofstrasse 1 Zürich", [a, b])[0]["name"], "Bahnhofstrasse 1")


class StreetTest(unittest.TestCase):
    """2026-11-04: the Lindenweg in Neustadt-Altdorf was not found (a namesake in Dormagen came first)."""
    HOME = {"name": "Lindenweg", "detail": "Altdorf, Hessen, Deutschland", "kind": "residential", "lat": 50.4,
            "lon": 9.0, "rank": 1, "where": "Lindenweg, Altdorf, Neustadt, Musterkreis, Hessen, 12345, Deutschland"}
    FAR = {"name": "Lindenweg", "detail": "Straße in Dormagen", "kind": "wikipedia", "lat": 51.1, "lon": 6.8, "rank": 1}

    def test_the_town_in_the_whole_address_counts(self):
        r = geocode.merge("Lindenweg Neustadt-Altdorf", [self.FAR, self.HOME])
        self.assertEqual([x["detail"] for x in r], [self.HOME["detail"]])

    def test_of_many_streets_the_nearest(self):
        other = {**self.HOME, "detail": "Eußenheim", "lat": 50.0, "lon": 9.8, "where": ""}
        self.assertEqual(geocode.merge("Lindenweg", [other, self.HOME], 50.41, 9.01)[0]["detail"], self.HOME["detail"])

    def test_enter_takes_no_street_without_the_full_search(self):
        self.assertNotIn("sure", geocode.merge("Lindenweg", [self.FAR])[0])
        self.assertNotIn("sure", geocode.merge("Hauptstr. 5", [{**self.FAR, "name": "Hauptstr. 5"}])[0])
        place = {"name": "Umbrailpass", "detail": "", "lat": 46.5, "lon": 10.4}
        self.assertTrue(geocode.merge("Umbrailpass", [place])[0]["sure"])

    def test_a_street_alone_is_also_looked_for_nearby(self):
        net = Answers({})
        geocode._cache.clear()
        geocode.search("Lindenweg", 50.4, 9.0, net=net, country="DE", full=True)
        self.assertTrue([u for u in net.urls if "nominatim" in u and "bounded=1" in u])


class StatusBarRoadTest(unittest.TestCase):
    """2026-11-05 (Tann2019): on I-94 near Huntley MT the status bar never named the road: the Interstate has a number
    (Overture road_routes "us:i|94"), no name."""

    @staticmethod
    def roads(rows):
        """A roads layer of rows (id, class, name, [(lon, lat), ...]), the fields nearest_street reads."""
        import numpy as np
        pts = [np.asarray(r[3], np.float64) for r in rows]
        off = np.r_[0, np.cumsum([len(p) for p in pts])]
        n = len(rows)
        return types.SimpleNamespace(kind=np.zeros(n), ids=np.array([r[0] for r in rows]),
                                     attrs={"class": np.array([r[1] for r in rows]),
                                            "name": np.array([r[2] for r in rows], dtype=object)},
                                     part_off=np.arange(n + 1), ring_off=np.arange(n + 1), coord_off=off,
                                     lonlat=np.concatenate(pts))

    def test_route_labels(self):
        self.assertEqual(geocode.route_label("us:i", "94"), "I-94")
        self.assertEqual(geocode.route_label("US:US", "212"), "US 212")
        self.assertEqual(geocode.route_label("us:mt", "3"), "MT 3")
        self.assertEqual(geocode.route_label("e-road", "E 45"), "E 45")
        self.assertEqual(geocode.road_route("us:us|212|0.0|1.0;us:i|94|0.0|1.0"), "I-94")
        self.assertEqual(geocode.road_route(""), "")

    def test_an_interstate_without_a_name_is_called_by_its_number(self):
        lat, lon = 45.87295, -108.33252
        f = self.roads([("mw", "motorway", None, [(lon - 0.01, lat), (lon + 0.01, lat)]),
                        ("st", "residential", "Main Street", [(lon - 0.01, lat + 0.0003), (lon + 0.01, lat + 0.0003)])])
        self.assertEqual(geocode.nearest_street([f], lat, lon), "Main Street", "no numbers: the named street")
        self.assertEqual(geocode.nearest_street([f], lat, lon, routes={"mw": "us:i|94|0.0|1.0"}), "I-94")
        named = self.roads([("mw", "motorway", "Purple Heart Trail", [(lon - 0.01, lat), (lon + 0.01, lat)]),
                            ("lo", "primary", "Old Highway 10", [(lon - 0.01, lat + 0.0001), (lon + 0.01, lat + 0.0001)])])
        routes = {"mw": "us:i|94|0.0|1.0", "lo": "us:mt|312|0.0|1.0"}
        self.assertEqual(geocode.nearest_street([named], lat, lon, routes=routes), "I-94", "a motorway: its number")
        self.assertEqual(geocode.nearest_street([named], lat + 0.0001, lon, routes=routes), "Old Highway 10",
                         "other roads: their name")


class LandmarkTest(unittest.TestCase):
    def test_a_landmark_beats_a_like_address(self):
        house = {"name": "Pennsylvania Avenue 1600", "detail": "Bremerton, Washington", "kind": "house", "lat": 47.5,
                 "lon": -122.6, "rank": 1, "importance": 0.00001}
        white = {"name": "White House", "detail": "Pennsylvania Avenue Northwest 1600, Washington", "kind": "government",
                 "lat": 38.9, "lon": -77.0, "rank": 4, "importance": 0.696}
        self.assertEqual(geocode.merge("Pennsylvania Avenue 1600 Washington", [house, white])[0]["name"], "White House")


class SuperlativeTest(unittest.TestCase):
    def test_the_question(self):
        for q in ("höchste Brücke Schweiz", "höchste Brücke der Schweiz", "highest bridge in Switzerland",
                  "tallest bridge switzerland", "die hoechste bruecke in der schweiz"):
            self.assertEqual(geocode.superlative(q), ("bridge_height", "CH"), q)
        self.assertEqual(geocode.superlative("längster Tunnel Deutschlands"), ("tunnel_length", "DE"))
        self.assertEqual(geocode.superlative("longest bridge in the world"), ("bridge_length", ""))
        self.assertEqual(geocode.superlative("highest overpass"), ("bridge_height", None))

    def test_not_the_question(self):
        for q in ("Hauptstraße 5", "Golden Gate Bridge", "highest bridge cafe", "längste Straße"):
            self.assertIsNone(geocode.superlative(q), q)

    def test_answered_without_the_network(self):
        class Boom:
            def get(self, *a, **k):
                raise AssertionError("the table is in the app")
        r = geocode.search("höchste Brücke Schweiz", 46.8, 8.2, net=Boom(), country="CH")
        self.assertTrue(r)
        self.assertIn("#1 in CH", r[0]["detail"])
        self.assertTrue(45.8 < r[0]["lat"] < 47.9 and 5.9 < r[0]["lon"] < 10.6)

    def test_no_country_named_the_players_first(self):
        r = geocode.superlative_results("bridge_height", None, "CH", "en")
        self.assertIn("in CH", r[0]["detail"])
        self.assertTrue(any("the world" in x["detail"] for x in r))


if __name__ == "__main__":
    unittest.main()
