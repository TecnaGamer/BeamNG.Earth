"""geocode: the map's address search (Photon) request and response handling."""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geocode  # noqa: E402
import netfetch  # noqa: E402


class FakeNet:
    """Captures the requested URL; answers with an empty Photon-shaped response."""
    def __init__(self):
        self.urls = []

    def get(self, url, kind="demand", lane=None):
        self.urls.append(url)
        self.lane = lane
        return types.SimpleNamespace(body=b'{"features": []}')


class LangForTest(unittest.TestCase):
    def test_by_country(self):
        for cc in ("DE", "AT", "CH", "li"):
            self.assertEqual(geocode.lang_for(cc), "de", cc)
        self.assertEqual(geocode.lang_for("FR"), "fr")
        self.assertEqual(geocode.lang_for("IT"), "it")
        for cc in ("US", "CZ", "PL", "NL", "SI", None, ""):
            self.assertEqual(geocode.lang_for(cc), "en", cc)

    def test_search_and_reverse_pick_it_up(self):
        net = FakeNet()
        geocode.search("Koeln", 50.9375, 6.9603, net=net, country="DE")
        self.assertEqual(parse_qs(urlparse(net.urls[0]).query)["lang"], ["de"])
        net = FakeNet()
        geocode.search("Praha", 50.08, 14.42, net=net, country="CZ")      # (inside the PR's DACH box)
        self.assertEqual(parse_qs(urlparse(net.urls[0]).query)["lang"], ["en"])
        net = FakeNet()
        geocode.search("fells point", 39.2904, -76.6122, net=net)
        self.assertEqual(parse_qs(urlparse(net.urls[0]).query)["lang"], ["en"])
        net = FakeNet()
        geocode.reverse(46.0207, 7.7491, net=net, country="CH")
        self.assertEqual(parse_qs(urlparse(net.urls[0]).query)["lang"], ["de"])


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

    def test_default_urls_keep_their_keys(self):
        # The default URL is also the existing cache key; the custom endpoint must not change it.
        with patch.dict(os.environ):
            os.environ.pop(geocode.PHOTON_BASE_ENV, None)
            self.assertEqual(
                geocode.url(" Koeln ", 50.9375, 6.9603, lang="de"),
                "https://photon.komoot.io/api/?q=Koeln&limit=7&lang=de&lat=50.9375&lon=6.9603")
            self.assertEqual(
                geocode.reverse_url(46.0207, 7.7491, lang="de"),
                "https://photon.komoot.io/reverse?lat=46.02&lon=7.75&lang=de&limit=1")

    def test_custom_base_path_used_for_search_and_reverse(self):
        with patch.dict(os.environ, {geocode.PHOTON_BASE_ENV: "http://127.0.0.1:2322/geo/v1%20blue/"}):
            net = FakeNet()
            geocode.search("Koeln", 50.9375, 6.9603, net=net, country="DE")
            self.assertEqual(
                net.urls[0], "http://127.0.0.1:2322/geo/v1%20blue/api?"
                "q=Koeln&limit=7&lang=de&lat=50.9375&lon=6.9603")
            self.assertIsNone(net.lane)
            geocode.reverse(46.0207, 7.7491, net=net, country="CH")
            self.assertEqual(
                net.urls[1], "http://127.0.0.1:2322/geo/v1%20blue/reverse?"
                "lat=46.02&lon=7.75&lang=de&limit=1")
            self.assertEqual(net.lane, "reverse")

    def test_base_variants_and_invalid_settings(self):
        for base, expected in (
            ("http://[::1]:2322", "http://[::1]:2322/api"),
            ("https://example.org/a%2Fb///", "https://example.org/a%2Fb/api"),
        ):
            with self.subTest(base=base), patch.dict(os.environ, {geocode.PHOTON_BASE_ENV: base}):
                self.assertEqual(geocode.url("abc").split("?", 1)[0], expected)
        for base in ("", "   ", "example.org", "ftp://example.org/a", "http://[::1",
                     "http://example.org:notaport", "https://user:very-secret@example.org/a",
                     "https://example.org/a?key=very-secret", "https://example.org/a#very-secret"):
            with self.subTest(base=base), patch.dict(os.environ, {geocode.PHOTON_BASE_ENV: base}):
                for make in (lambda: geocode.url("abc"), lambda: geocode.reverse_url(1, 2)):
                    with self.assertRaises(geocode.InvalidEndpoint) as caught:
                        make()
                    self.assertNotIn("very-secret", str(caught.exception))
                    self.assertNotIn("very-secret", geocode.error_text(caught.exception))

    def test_error_classification_never_exposes_url_or_exception(self):
        username, password = "private-user-456", "private-password-123"
        path_secret, query_secret = "private-path-abc", "private-token-789"
        detail = f"https://{username}:{password}@example.org/{path_secret}?token={query_secret}"
        rate = netfetch.RateLimited(detail, status=429, retry_after=2.1)
        unavailable = netfetch.RateLimited(detail, status=503, retry_after=12)
        breaker = netfetch.HostUnavailable(detail, retry_after=1)
        cases = (
            (netfetch.FetchFailed(detail, status=403), ("403", "refus"), ("offline",)),
            (netfetch.FetchFailed(detail, status=401), ("401", "refus"), ("offline",)),
            (rate, ("429", "limit"), ("offline",)),
            (unavailable, ("503",), ("offline",)),
            (netfetch.RateLimited(detail), ("limit", "policy"), ("offline",)),
            (netfetch.Offline(detail), ("offline",), ("403", "429")),
            (breaker, ("circuit",), ("offline",)),
            (netfetch.BudgetExceeded(detail), ("budget",), ("offline",)),
            (netfetch.PolicyDenied(detail), ("policy",), ("offline",)),
            (netfetch.FetchFailed(detail), ("network",), ("offline",)),
            (netfetch.NotFound(detail, status=404), ("404",), ("offline",)),
            (netfetch.BadResponse(detail, status=200), ("invalid",), ()),
            (json.JSONDecodeError(detail, "not json", 0), ("invalid",), ()),
            (RuntimeError(detail), ("error",), ()),
        )
        for error, required, forbidden in cases:
            with self.subTest(error=type(error).__name__, status=getattr(error, "status", None)):
                text = geocode.error_text(error).lower()
                for part in required:
                    self.assertIn(part, text)
                for part in forbidden + ("https://", "example.org", username, password, path_secret, query_secret):
                    self.assertNotIn(part, text)
        for error, seconds in ((rate, 3), (unavailable, 12), (breaker, 1)):
            self.assertRegex(geocode.error_text(error).lower(), rf"\b{seconds}\s*(?:s|seconds?)\b")


if __name__ == "__main__":
    unittest.main()
