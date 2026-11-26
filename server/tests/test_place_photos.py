"""placephotos.py and earthd's 'place_photos': the modding API's place photos (api.placePhotos in the mod).
The request's checks (places, ids, positions, a token only when shaped like Mapillary's); which Mapillary photo shows
a storefront (distance, aim, age, no panoramas) and which Commons file names a place (never on the kind of place
alone, no drawings); the lookups over a fake network: Commons first, Mapillary only with a token and the token only
in the header, the picture shrunk to a JPEG in photos/ and its credit, answers kept on disk (no second lookup; a
"none" asked again once a token came), only photo hosts downloaded, a failed source or download is "missing", the
oldest files pruned; earthd answers once and says why when it cannot."""
import asyncio
import io
import json
import math
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import placephotos as pp  # noqa: E402

SHOP = (45.4039, -75.6890)
NOW = 1795262400.0                  # 2026-11-21


def at(e, n):
    lat, lon = SHOP
    return [lon + math.degrees(e / (pp.R * math.cos(math.radians(lat)))), lat + math.degrees(n / pp.R)]


_nid = [0]


def cam(e, n, heading, **extra):
    _nid[0] += 1
    row = {"id": _nid[0], "thumb_1024_url": f"https://scontent-yyz1-1.xx.fbcdn.net/m1/{_nid[0]}.jpg",
           "computed_geometry": {"coordinates": at(e, n)}, "computed_compass_angle": heading,
           "captured_at": 1717200000000, "creator": {"username": f"u{_nid[0]}"}}
    row.update(extra)
    return row


def page(title, i=1, artist="Ann"):
    return {"title": "File:" + title, "index": i, "imageinfo": [{
        "thumburl": "https://upload.wikimedia.org/thumb/" + title.replace(" ", "_"), "url": "https://upload.wikimedia.org/o",
        "extmetadata": {"Artist": {"value": f'<a href="x">{artist}</a>'}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}


class Request(unittest.TestCase):
    def test_checks(self):
        ok, tok = pp.request({"places": [{"id": "p1", "lat": 45.4, "lon": -75.6, "name": "A"}], "mapillaryToken": "MLY|12|ab3F"})
        self.assertEqual(ok, [("p1", 45.4, -75.6, "A")])
        self.assertEqual(tok, "MLY|12|ab3F")
        self.assertEqual(pp.request({"places": [{"id": "p1", "lat": 1, "lon": 2}], "mapillaryToken": "x; rm"})[1], None)
        self.assertEqual(pp.request({"places": [{"id": "p1", "lat": 1, "lon": 2, "name": 5}]})[0][0][3], "")
        for bad, why in (({}, "no places"), ({"places": []}, "no places"),
                         ({"places": [{"id": "p", "lat": 1, "lon": 1}] * 9}, "at most 8"),
                         ({"places": [{"id": "", "lat": 1, "lon": 1}]}, "place 1"),
                         ({"places": [{"id": "x" * 65, "lat": 1, "lon": 1}]}, "place 1"),
                         ({"places": [{"id": "p", "lat": 91, "lon": 1}]}, "place 1"),
                         ({"places": [{"id": "p", "lat": True, "lon": 1}]}, "place 1"),
                         ({"places": [{"id": "p", "lat": float("nan"), "lon": 1}]}, "place 1"),
                         ({"places": ["p"]}, "place 1")):
            got, msg = pp.request(bad)
            self.assertIsNone(got)
            self.assertIn(why, msg)


class Pick(unittest.TestCase):
    def test_geometry(self):
        self.assertAlmostEqual(pp.bearing(0, 0, 1, 0), 0)
        self.assertAlmostEqual(pp.bearing(0, 0, 0, 1), 90)
        self.assertAlmostEqual(pp.across(0), 0.5)
        self.assertGreater(pp.across(20), 0.5)
        self.assertEqual(pp.across(89), 1.0)
        w, s, e, n = map(float, pp.bbox(*SHOP, 55).split(","))
        self.assertTrue(w < SHOP[1] < e and s < SHOP[0] < n)

    def test_mapillary(self):
        good = cam(-7.4, -18.3, 0)                      # 20 m short of the shop, 7 m to the side, driving north
        rows = [cam(0, -40, 0), cam(-2, -3, 30), cam(0, -70, 0), cam(-7.4, -18.3, 180), cam(-7.4, -18.3, 0, is_pano=True),
                cam(-7.4, -18.3, 0, captured_at=1391212800000), good]
        im, aim = pp.pick_mapillary(rows, *SHOP, NOW * 1000)
        self.assertIs(im, good)
        self.assertTrue(10 < aim < 30)
        self.assertIsNone(pp.pick_mapillary(rows[1:5], *SHOP, NOW * 1000))
        self.assertIsNotNone(pp.pick_mapillary([cam(-7.4, -18.3, None, compass_angle=0)], *SHOP, NOW * 1000))
        self.assertIsNone(pp.pick_mapillary([{"id": 1}, None, "x", cam(-7.4, -18.3, 0, thumb_1024_url=None)], *SHOP, 0))
        self.assertIsNone(pp.pick_mapillary(None, *SHOP, 0))

    def test_commons(self):
        pages = {"1": page("Some other church Ottawa.jpg"), "2": page("St. Giles Presbyterian Church - Bank Street.jpg", 2),
                 "3": page("Giles map.svg", 3)}
        self.assertIn("Giles", pp.pick_commons(pages, "St Giles Presbyterian Church")["title"])
        self.assertIsNone(pp.pick_commons({"1": page("Fourth Avenue Baptist Church.jpg")}, "Hope For All Nations Church"))
        self.assertIsNone(pp.pick_commons({"1": page("Church in Ottawa.jpg")}, "Downtown Church"))
        self.assertIsNone(pp.pick_commons({"1": page("Ottawa City Hall.jpg")}, "Ottawa Mission"))
        self.assertIsNotNone(pp.pick_commons({"1": page("Château Laurier.jpg")}, "Chateau Laurier Hotel"))
        self.assertIsNone(pp.pick_commons({"1": page("Map of Aberdeen Pavilion.svg")}, "Aberdeen Pavilion"))
        self.assertIsNone(pp.pick_commons(None, "x"))
        self.assertIsNone(pp.pick_commons({}, ""))


def jpeg(w=1024, h=768):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 60)).save(out, "JPEG")
    return out.getvalue()


class FakeNet:
    def __init__(self, commons=None, mly=None, fail=()):
        self.commons = commons if commons is not None else {"query": {"pages": {}}}
        self.mly = mly if mly is not None else {"data": []}
        self.fail = set(fail)
        self.calls = []

    def get(self, url, kind="demand", headers=None):
        self.calls.append((url, dict(headers or {})))
        host = urlsplit(url).hostname
        if host in self.fail:
            raise IOError("down")
        if host == "commons.wikimedia.org":
            body = json.dumps(self.commons).encode()
        elif host == "graph.mapillary.com":
            body = json.dumps(self.mly).encode()
        else:
            body = jpeg()
        return types.SimpleNamespace(status=200, body=body)


class Lookups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def photos(self, net, t=NOW):
        return pp.Photos(net, self.root, clock=lambda: t)

    def test_commons_first_and_the_file(self):
        net = FakeNet(commons={"query": {"pages": {"1": page("Aberdeen Pavilion 2019.jpg", artist="Ross")}}})
        found, missing = self.photos(net).find([("p1", *SHOP, "Aberdeen Pavilion")], "MLY|1|ab")
        self.assertEqual(missing, [])
        ph = found[0]
        self.assertEqual((ph["id"], ph["source"], ph["by"], ph["licence"]), ("p1", "Wikimedia Commons", "Ross", "CC BY-SA 4.0"))
        self.assertTrue(ph["file"].startswith(pp.VFS_DIR) and ph["file"].endswith(".jpg"))
        disk = os.path.join(self.root, "photos", ph["file"][len(pp.VFS_DIR):])
        from PIL import Image
        with Image.open(disk) as im:
            self.assertTrue(im.width <= pp.MAX_W and im.height <= pp.MAX_H and im.format == "JPEG")
        self.assertFalse(any("mapillary" in u for u, _ in net.calls), "Commons named it: Mapillary not asked")

    def test_mapillary_with_token_in_the_header_only(self):
        good = cam(-7.4, -18.3, 0)
        net = FakeNet(mly={"data": [good]})
        found, _ = self.photos(net).find([("p2", *SHOP, "Corner Store")], "MLY|1|ab")
        self.assertEqual(found[0]["source"], "Mapillary")
        self.assertEqual(found[0]["by"], f"u{good['id']}")
        self.assertEqual(found[0]["year"], 2024)
        self.assertGreater(found[0]["fx"], 0.5)
        self.assertEqual(found[0]["zoom"], pp.ZOOM)
        mly = [(u, h) for u, h in net.calls if "graph.mapillary.com" in u]
        self.assertEqual(mly[0][1].get("Authorization"), "OAuth MLY|1|ab")
        self.assertTrue(all("MLY" not in u for u, _ in net.calls), "the token never in a URL")
        q = parse_qs(urlsplit(mly[0][0]).query)
        self.assertIn("bbox", q)

    def test_no_token_no_mapillary_then_asked_again_with_one(self):
        net = FakeNet(mly={"data": [cam(-7.4, -18.3, 0)]})
        p = self.photos(net)
        self.assertEqual(p.find([("p3", *SHOP, "Corner Store")], None), ([], ["p3"]))
        self.assertFalse(any("mapillary" in u for u, _ in net.calls))
        n = len(net.calls)
        self.assertEqual(p.find([("p3", *SHOP, "Corner Store")], None), ([], ["p3"]))
        self.assertEqual(len(net.calls), n, "a remembered none is not asked again")
        found, _ = p.find([("p3", *SHOP, "Corner Store")], "MLY|1|ab")
        self.assertEqual(found[0]["source"], "Mapillary", "a token came: asked again")

    def test_kept_on_disk(self):
        net = FakeNet(commons={"query": {"pages": {"1": page("Aberdeen Pavilion.jpg")}}})
        self.photos(net).find([("p1", *SHOP, "Aberdeen Pavilion")])
        n = len(net.calls)
        found, _ = self.photos(net).find([("p1", *SHOP, "Aberdeen Pavilion")])      # (a new server: from disk)
        self.assertEqual((len(net.calls), found[0]["source"]), (n, "Wikimedia Commons"))
        found, _ = self.photos(net, t=NOW + pp.KEEP_S + 1).find([("p1", *SHOP, "Aberdeen Pavilion")])
        self.assertGreater(len(net.calls), n, "looked up again after KEEP_S")

    def test_failures_are_missing(self):
        net = FakeNet(fail={"commons.wikimedia.org", "graph.mapillary.com"})
        p = self.photos(net)
        self.assertEqual(p.find([("p1", *SHOP, "X Y")], "MLY|1|ab"), ([], ["p1"]))
        n = len(net.calls)
        p.find([("p1", *SHOP, "X Y")], "MLY|1|ab")
        self.assertGreater(len(net.calls), n, "a source down is not remembered as no photo")
        net = FakeNet(commons={"query": {"pages": {"1": page("Aberdeen Pavilion.jpg")}}}, fail={"upload.wikimedia.org"})
        p = self.photos(net)
        self.assertEqual(p.find([("p1", *SHOP, "Aberdeen Pavilion")]), ([], ["p1"]))
        n = len(net.calls)
        p.find([("p1", *SHOP, "Aberdeen Pavilion")])
        self.assertGreater(len(net.calls), n, "a failed download is asked again")
        net = FakeNet(commons={"query": {"pages": {"1": {**page("Aberdeen Pavilion.jpg"), "imageinfo": [{
            "thumburl": "https://evil.example.com/x.jpg", "url": "https://evil.example.com/x.jpg"}]}}}})
        self.assertEqual(self.photos(net).find([("p9", *SHOP, "Aberdeen Pavilion")]), ([], ["p9"]))
        self.assertFalse(any("evil.example.com" in u for u, _ in net.calls), "only photo hosts are downloaded")

    def test_prune(self):
        p = self.photos(FakeNet())
        os.makedirs(p.dir)
        for i in range(12):
            for ext in (".jpg", ".json"):
                f = os.path.join(p.dir, f"k{i:02d}{ext}")
                open(f, "w").close()
                os.utime(f, (NOW + i, NOW + i))
        self.assertEqual(p.prune(keep=5), 14)
        self.assertEqual(sorted(n for n in os.listdir(p.dir) if n.endswith(".jpg")), [f"k{i:02d}.jpg" for i in range(7, 12)])


class Earthd(unittest.TestCase):
    def conn(self, net, root):
        import earthd
        from concurrent.futures import ThreadPoolExecutor
        c = object.__new__(earthd.Connection)
        sent = []
        c.send = sent.append
        c.s = types.SimpleNamespace(net=net, root=root, api_photo_pool=ThreadPoolExecutor(1), place_photos=None)
        return c, sent

    def test_answer_and_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            net = FakeNet(commons={"query": {"pages": {"1": page("Aberdeen Pavilion.jpg")}}})
            c, sent = self.conn(net, root)
            asyncio.run(c.place_photos({"id": 7, "places": [{"id": "p1", "lat": SHOP[0], "lon": SHOP[1],
                                                            "name": "Aberdeen Pavilion"},
                                                           {"id": "p2", "lat": SHOP[0], "lon": SHOP[1], "name": "Q"}]}))
            self.assertEqual((sent[-1]["type"], sent[-1]["id"]), ("place_photos_result", 7))
            self.assertEqual([p["id"] for p in sent[-1]["photos"]], ["p1"])
            self.assertEqual(sent[-1]["missing"], ["p2"])
            self.assertNotIn("error", sent[-1])
            asyncio.run(c.place_photos({"id": 8}))
            self.assertEqual((sent[-1]["error"], sent[-1]["photos"]), ("no places", []))


class ServerAttributes(unittest.TestCase):
    """The modding API's lookups run on thread pools of their own, kept on the Server. In game no photo ever came
    (2026-11-21): place_photos ran on `photo_pool`, a name the server already had for the blocks' photos, which main()
    replaces with a process pool; the lookup could not be sent to another process ("cannot pickle
    'TruststoreSSLSocket' object"). Each of these names is assigned exactly once in earthd.py."""
    def test_assigned_once(self):
        import ast
        src = Path(__file__).resolve().parents[1] / "earthd.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        counts = {}
        for n in ast.walk(tree):
            targets = n.targets if isinstance(n, ast.Assign) else ([n.target] if isinstance(n, ast.AnnAssign) else [])
            for tg in targets:
                for e in (tg.elts if isinstance(tg, ast.Tuple) else [tg]):
                    if isinstance(e, ast.Attribute):
                        counts[e.attr] = counts.get(e.attr, 0) + 1
        for name in ("api_photo_pool",):
            self.assertEqual(counts.get(name), 1, name)
        for name in ("place_photos",):       # set once in __init__, once on the first request
            self.assertEqual(counts.get(name), 2, name)


if __name__ == "__main__":
    unittest.main()
