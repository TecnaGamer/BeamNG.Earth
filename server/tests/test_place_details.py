"""placedetails.py and earthd's 'place_details': the modding API's place details (api.placeDetails in the mod).
The request's checks and caps; an MVT place tile decoded (gzip or not, other layers skipped, tags by index); a place's
Overture tags turned into its details (JSON columns parsed, strings cleaned and capped, only http(s) links and real
emails, lists capped and deduplicated, sources' licences); the lookup reads only the tile on disk (a missing tile, a
place not in its tile and a damaged tile are "missing", nothing fetched), keeps decoded tiles in an LRU; earthd answers
once, says why when it cannot (bad request, no tiles)."""
import asyncio
import gzip
import json
import struct
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mapcells  # noqa: E402
import placedetails as pd  # noqa: E402

LAT, LON = 45.4085, -75.6880        # Bank Street, Ottawa


# --- a tiny MVT encoder (the test's own, so the decoder is checked against the format, not against itself) --------
def _vi(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _f(field, wire, payload):
    key = _vi(field << 3 | wire)
    if wire == 0:
        return key + _vi(payload)
    if wire in (1, 5):
        return key + payload
    return key + _vi(len(payload)) + payload


def _val(v):
    if isinstance(v, bool):
        return _f(7, 0, int(v))
    if isinstance(v, float):
        return _f(3, 1, struct.pack("<d", v))
    if isinstance(v, int):
        return _f(4, 0, v)
    return _f(1, 2, v.encode())


def mvt_layer(name, features):
    keys, vals, body = [], [], b""
    for tags in features:
        idx = []
        for k, v in tags.items():
            if k not in keys:
                keys.append(k)
            if v not in vals:
                vals.append(v)
            idx += [keys.index(k), vals.index(v)]
        packed = b"".join(_vi(i) for i in idx)
        geom = b"".join(_vi(i) for i in (9, 50, 50))          # MoveTo(25, 25)
        body += _f(2, 2, _f(1, 0, 1) + _f(2, 2, packed) + _f(3, 0, 1) + _f(4, 2, geom))
    lay = _f(15, 0, 2) + _f(1, 2, name.encode()) + body
    lay += b"".join(_f(3, 2, k.encode()) for k in keys) + b"".join(_f(4, 2, _val(v)) for v in vals) + _f(5, 0, 4096)
    return _f(3, 2, lay)


def overture_tags(pid, name, **extra):
    t = {"id": pid, "@name": name, "names": json.dumps({"primary": name}), "confidence": 0.97,
         "basic_category": "barber_shop",
         "taxonomy": json.dumps({"primary": "barber", "hierarchy": ["beauty_and_spa", "barber_shop", "barber"]}),
         "addresses": json.dumps([{"country": "CA", "freeform": "736 Bank St", "locality": "Ottawa",
                                   "postcode": "K1S 3V4", "region": "ON"}]),
         "phones": json.dumps(["+16135550142"]), "websites": json.dumps(["https://twochairs.example/"]),
         "socials": json.dumps(["https://www.facebook.com/123"]), "emails": json.dumps(["hi@twochairs.example"]),
         "brand": json.dumps({"names": {"primary": "Two Chairs"}, "wikidata": "Q1"}),
         "sources": json.dumps([{"resource": "meta", "license": "CDLA-Permissive-2.0", "record_id": "9"},
                                {"resource": "meta", "license": "CDLA-Permissive-2.0", "record_id": "10"}]),
         "operating_status": "open", "version": 3}
    t.update(extra)
    return t


class Request(unittest.TestCase):
    def test_checks(self):
        ok, why = pd.request({"places": [{"id": "a", "lat": LAT, "lon": LON}, {"id": "a", "lat": LAT, "lon": LON}]})
        self.assertEqual(ok, [("a", LAT, LON)], "a place asked twice is looked up once")
        for msg, want in (({}, "no places asked"), ({"places": []}, "no places asked"),
                          ({"places": [{"id": str(i), "lat": 1, "lon": 2} for i in range(pd.DETAILS_MAX + 1)]},
                           f"at most {pd.DETAILS_MAX} places at once"),
                          ({"places": ["a"]}, "a place must be {id, lat, lon}"),
                          ({"places": [{"id": "a", "lat": "x", "lon": 1}]}, "bad coordinates"),
                          ({"places": [{"id": "a", "lat": 89.9, "lon": 1}]}, "bad coordinates"),
                          ({"places": [{"id": "a", "lat": float("nan"), "lon": 1}]}, "bad coordinates"),
                          ({"places": [{"id": "", "lat": 1, "lon": 1}]}, "bad place id"),
                          ({"places": [{"id": 7, "lat": 1, "lon": 1}]}, "bad place id"),
                          ({"places": [{"id": "x" * (pd.ID_MAX + 1), "lat": 1, "lon": 1}]}, "bad place id")):
            got, why = pd.request(msg)
            self.assertIsNone(got, msg)
            self.assertEqual(why, want, msg)


class Decode(unittest.TestCase):
    def test_place_layer_only_gzip_or_not(self):
        raw = mvt_layer("other", [{"id": "zz", "x": 1}]) + mvt_layer("place", [overture_tags("p1", "Two Chairs"),
                                                                              overture_tags("p2", "Halfmoon")])
        for blob in (raw, gzip.compress(raw)):
            got = pd.decode_tile(blob)
            self.assertEqual(sorted(got), ["p1", "p2"])
            self.assertEqual(got["p1"]["@name"], "Two Chairs")
            self.assertEqual(got["p2"]["version"], 3)
            self.assertAlmostEqual(got["p1"]["confidence"], 0.97)
        self.assertEqual(pd.decode_tile(b""), {})

    def test_damaged(self):
        raw = mvt_layer("place", [overture_tags("p1", "A")])
        with self.assertRaises(ValueError):
            pd.decode_tile(raw[:len(raw) // 2])


class Details(unittest.TestCase):
    def test_fields(self):
        d = pd.details(overture_tags("p1", "Two Chairs Barbers"))
        self.assertEqual(d["name"], "Two Chairs Barbers")
        self.assertEqual(d["address"], {"freeform": "736 Bank St", "locality": "Ottawa", "postcode": "K1S 3V4",
                                        "region": "ON", "country": "CA"})
        self.assertEqual(d["phones"], ["+16135550142"])
        self.assertEqual(d["websites"], ["https://twochairs.example/"])
        self.assertEqual(d["emails"], ["hi@twochairs.example"])
        self.assertEqual(d["brand"], {"name": "Two Chairs", "wikidata": "Q1"})
        self.assertEqual((d["category"], d["taxonomy"]), ("barber_shop", "barber"))
        self.assertEqual(d["hierarchy"], ["beauty_and_spa", "barber_shop", "barber"])
        self.assertEqual(d["sources"], [{"dataset": "meta", "license": "CDLA-Permissive-2.0"}], "sources deduplicated")
        self.assertEqual((d["open"], d["confidence"]), ("open", 0.97))

    def test_cleaned_and_capped(self):
        d = pd.details(overture_tags(
            "p1", "Bad\x00Name" + "x" * 400,
            websites=json.dumps(["javascript:alert(1)", "ftp://x.example", "http://ok.example/a", "http://ok.example/a"]
                                + [f"https://w{i}.example" for i in range(9)]),
            emails=json.dumps(["not an email", "a@b.ca"]), phones="not json", addresses="[]",
            names="{broken", brand=json.dumps({"names": "x"}), confidence="nan"))
        self.assertNotIn("\x00", d["name"])
        self.assertEqual(len(d["name"]), pd.TEXT_MAX)
        self.assertEqual(d["websites"][0], "http://ok.example/a", "only http(s), each once")
        self.assertEqual(len(d["websites"]), pd.LIST_MAX)
        self.assertEqual(d["emails"], ["a@b.ca"])
        self.assertEqual(d["phones"], [])
        self.assertEqual(d["address"]["freeform"], "")
        self.assertEqual(d["brand"]["name"], "")
        self.assertIsNone(d["confidence"])
        self.assertTrue(d["name"].startswith("Bad Name"), "a names column that does not parse: @name")

    def test_json_roundtrip(self):
        json.dumps(pd.details(overture_tags("p1", "A")))       # (it goes to the game as JSON)


class Lookup(unittest.TestCase):
    def setUp(self):
        c = mapcells.cell_of(LAT, LON)
        self.key = (c.z, c.x, c.y)
        self.reads = []
        c2 = mapcells.cell_of(10.0, 10.0)
        self.disk = {self.key: gzip.compress(mvt_layer("place", [overture_tags("p1", "Two Chairs"),
                                                                 overture_tags("p2", "Halfmoon")])),
                     (c2.z, c2.x, c2.y): mvt_layer("place", [overture_tags("p7", "Elsewhere")])}

        def tile_bytes(z, x, y):
            self.reads.append((z, x, y))
            return self.disk.get((z, x, y))
        self.lk = pd.Lookup(tile_bytes, kept=1)

    def test_found_missing_order_and_cache(self):
        found, missing = self.lk.find([("p2", LAT, LON), ("nope", LAT, LON), ("p1", LAT, LON),
                                       ("p1", 10.0, 10.0)])
        self.assertEqual([d["name"] for d in found], ["Halfmoon", "Two Chairs"])
        self.assertEqual(missing, ["nope", "p1"], "not in its tile; not in that other tile")
        self.assertEqual(self.lk.find([("p8", 20.0, 20.0)]), ([], ["p8"]), "its tile not on disk")
        self.assertEqual(self.reads.count(self.key), 1, "a tile decoded once")
        self.lk.find([("p1", LAT, LON)])
        self.assertEqual(self.reads.count(self.key), 2, "kept=1: the other tile pushed it out")

    def test_damaged_tile_is_missing(self):
        self.disk[self.key] = b"\x1f\x8bgarbage"
        self.assertEqual(self.lk.find([("p1", LAT, LON)]), ([], ["p1"]))

    def test_empty_tile(self):
        self.disk[self.key] = b""
        self.assertEqual(self.lk.find([("p1", LAT, LON)]), ([], ["p1"]))


class Earthd(unittest.TestCase):
    def conn(self, mapdata):
        import earthd
        c = object.__new__(earthd.Connection)
        sent = []
        c.send = sent.append
        c.s = types.SimpleNamespace(mapdata=mapdata, api_pool=None)
        return c, sent

    def test_answer(self):
        raw = mvt_layer("place", [overture_tags("p1", "Two Chairs")])
        c0 = mapcells.cell_of(LAT, LON)
        arch = types.SimpleNamespace(cached=lambda z, x, y: raw if (z, x, y) == (c0.z, c0.x, c0.y) else None)
        md = types.SimpleNamespace(pmtiles=types.SimpleNamespace(archive=lambda theme: arch))
        c, sent = self.conn(md)
        asyncio.run(c.place_details({"id": 4, "places": [{"id": "p1", "lat": LAT, "lon": LON},
                                                         {"id": "p9", "lat": LAT, "lon": LON}]}))
        self.assertEqual(sent[-1]["type"], "place_details_result")
        self.assertEqual(sent[-1]["id"], 4)
        self.assertEqual([d["name"] for d in sent[-1]["details"]], ["Two Chairs"])
        self.assertEqual(sent[-1]["missing"], ["p9"])
        self.assertNotIn("error", sent[-1])

    def test_refusals(self):
        c, sent = self.conn(None)
        asyncio.run(c.place_details({"id": 1, "places": [{"id": "p1", "lat": LAT, "lon": LON}]}))
        self.assertIn("no place tiles", sent[-1]["error"])
        self.assertEqual(sent[-1]["missing"], ["p1"])
        c, sent = self.conn(types.SimpleNamespace(pmtiles=None))
        asyncio.run(c.place_details({"id": 2, "places": [{"id": "p1", "lat": LAT, "lon": LON}]}))
        self.assertIn("parquet", sent[-1]["error"])
        asyncio.run(c.place_details({"id": 3}))
        self.assertEqual((sent[-1]["error"], sent[-1]["details"]), ("no places asked", []))


if __name__ == "__main__":
    unittest.main()
