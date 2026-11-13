"""Australian nature strips (roads.au_verge): a low kerb at the road's edge, a grass verge, the footpath near the
property line (none on many minor streets). Every other country's sidewalks must come out exactly as before."""
import collections
import hashlib
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # (test_roads, also run as tests.test_nature_strips)
import furniture  # noqa: E402
import roads  # noqa: E402
import surface as sf  # noqa: E402
from test_roads import FRAME, features, flat  # noqa: E402

S = roads.Spec
LINE = np.array([[0.0, 0, 0], [60, 5, 0], [118, 0, 0]])
STRAIGHT = np.array([[0.0, 0, 0], [118, 0, 0]])
URBAN = lambda la, lo: np.ones(np.shape(la), bool)      # noqa: E731
RES = S(7.0, "paved", True, "residential", False)

CASES = {
    "res": RES,
    "res_walk4": RES._replace(walk=4.0),
    "res_walk18": RES._replace(walk=1.8),
    "res_both": RES._replace(sides=(1, -1)),
    "res_left": RES._replace(sides=(1,)),
    "res_none": RES._replace(sides=()),
    "res_rural": S(6.0, "paved", False, "residential", False),
    "living": S(6.0, "paved", True, "living_street", False),
    "uncl": S(6.0, "paved", True, "unclassified", False),
    "tert": S(8.0, "paved", True, "tertiary", False),
    "sec": S(10.0, "paved", True, "secondary", False),
    "trunk_ow": S(9.0, "paved", True, "trunk", True, 2.5, 1, 50.0),
}


def extras_digest(country):
    """A digest of roads.extras (sidewalk, gutter and paint rows) of every case, both traffic sides, three ids."""
    h = hashlib.sha1()
    for name, sp in CASES.items():
        for lh in (False, True):
            for fid in (None, "abc", "w123"):
                kw = {"fid": fid} if "fid" in roads.extras.__code__.co_varnames[:roads.extras.__code__.co_argcount] else {}
                ex = roads.extras(sp, LINE, 12.0, left_hand=lh, country=country, **kw)
                h.update(repr((name, lh, fid, [(sorted((k, v) for k, v in lk.items()), round(float(w), 4),
                                                [[round(float(v), 4) for v in p] for p in (ln[0], ln[-1])], len(ln))
                                               for lk, w, ln in ex])).encode())
    return h.hexdigest()[:12]


def grid(tag="both", ids=None):
    """Two crossing residential streets (east-west y = 100, north-south x = 120, joined at a node), the map's sidewalk
    tag on both (None: none)."""
    f = features([[(-300, 100), (120, 100), (300, 100)], [(120, -100), (120, 100), (120, 300)]],
                 ["residential", "residential"], ids=ids)
    if tag:
        f.attrs["osm_sidewalk"] = np.array([tag, tag])
    return f


def tile_digest(country, tag="both"):
    """A digest of a tile's sidewalk runs, corners (walks) and chunks of the crossing streets."""
    walks = []
    chunks = roads.build_tile(FRAME, 0, 0, [grid(tag)], flat, URBAN, walks=walks, country=country)
    h = hashlib.sha1()
    for w in walks:
        if "corner" in w:
            c = w["corner"]
            h.update(repr((sorted(c), sorted(w), [[round(float(v), 3) for v in p] for p in c["arc"]],
                           [[round(float(v), 3) for v in p] for p in c["outer"]], list(c.get("ramps", ())))).encode())
            continue
        h.update(repr((sorted(w), [[round(float(v), 3) for v in p] for p in w["pts"]], w["side"], w["caps"],
                       round(float(w["w"]), 3), w["fid"], list(w["ramps"]))).encode())
    h.update(repr([(c["m"], c["w"], len(c["n"])) for c in chunks]).encode())
    return h.hexdigest()[:12]


# digests of the code before the Australian change (the commit before it)
EXTRAS_BEFORE = {"US": "ae06168a501d", "CA": "3f6f26db158c", "GB": "10773d37f245", "IE": "10773d37f245",
                 "DE": "05a19fd2898b", "FR": "f6cde82f9d67", "SE": "84863f08afec", "NZ": "93665ee86aae",
                 "JP": "57bfa70bc4d1", "NL": "05a19fd2898b", "BR": "6f10dcc00bd0", None: "8975b35957b5"}
TILE_BEFORE = {"US": "aef10460cb6e", "CA": "aef10460cb6e", None: "aef10460cb6e", "GB": "567701f9def0",
               "DE": "567701f9def0", "NZ": "567701f9def0", "JP": "567701f9def0", "FR": "567701f9def0"}


class OtherCountriesTest(unittest.TestCase):
    def test_extras_of_other_countries_are_unchanged(self):
        for country, want in EXTRAS_BEFORE.items():
            self.assertEqual(extras_digest(country), want, f"{country}'s sidewalks / paint changed")

    def test_tiles_of_other_countries_are_unchanged(self):
        for country, want in TILE_BEFORE.items():
            self.assertEqual(tile_digest(country), want, f"{country}'s tile changed")
            self.assertEqual(tile_digest(country, None), want, f"{country}'s untagged tile changed")

    def test_the_rest_of_australia_s_roads_are_untouched(self):
        for cls in ("tertiary", "secondary", "primary"):
            sp = S(8.0, "paved", True, cls, False)
            self.assertIsNone(roads.au_verge(sp, "AU", "x"), cls)
            self.assertEqual(walk_rows(roads.extras(sp, STRAIGHT, 153.0, country="AU", fid="x")),
                             walk_rows(roads.extras(sp, STRAIGHT, -76.0, country="US", fid="x")), f"{cls}: the usual sidewalk")


def walk_rows(ex):
    return [(lk, round(float(w), 3), round(float(ln[0][1]), 3)) for lk, w, ln in ex if lk["m"] == roads.SIDEWALK["m"]]


class VergeLayoutTest(unittest.TestCase):
    def test_a_residential_street_gets_a_kerb_a_strip_and_a_set_back_footpath(self):
        ex = roads.extras(RES._replace(sides=(1, -1)), STRAIGHT, 153.0, country="AU", fid="x")
        rows = walk_rows(ex)
        self.assertEqual(len(rows), 4, "a kerb band and a footpath each side")
        for side in (1, -1):
            kerb = [r for r in rows if r[0]["side"] == side and not r[0].get("foot")]
            foot = [r for r in rows if r[0]["side"] == side and r[0].get("foot")]
            self.assertEqual((len(kerb), len(foot)), (1, 1))
            self.assertEqual(kerb[0][2], side * (3.5 + roads.AU_KERB_W / 2), "kerb on the road's edge")
            self.assertGreater(abs(foot[0][2]), abs(kerb[0][2]) + 2.0, "footpath set back behind the strip")
        self.assertEqual(len([e for e in ex if e[0]["m"] == roads.GUTTER["m"]]), 2, "the channel stays")

    def test_the_footpath_follows_the_maps_sidewalk_tag(self):
        for i in range(10):                 # a tagged street keeps its footpaths whatever its hash says
            for sides in ((1,), (-1,), (1, -1)):
                self.assertEqual(roads.au_verge(RES._replace(sides=sides), "AU", f"w{i}")["foot"], sides)
        self.assertIsNone(roads.au_verge(RES._replace(sides=()), "AU", "x"), "mapped without a sidewalk: no verge either")
        self.assertIsNone(roads.au_verge(RES._replace(sides=(1,)), "US", "x"))

    def test_an_untagged_minor_street_has_a_footpath_both_sides_one_or_none_by_a_hash(self):
        n = collections.Counter(len(roads.au_verge(RES, "AU", f"id{i}")["foot"]) for i in range(3000))
        self.assertTrue(n[0] and n[1] and n[2], "both sides, one or none all occur")
        self.assertEqual(roads.au_verge(RES, "AU", "id7"), roads.au_verge(RES, "AU", "id7"), "deterministic")
        none = next(f for f in (f"id{i}" for i in range(99)) if not roads.au_verge(RES, "AU", f)["foot"])
        rows = walk_rows(roads.extras(RES, STRAIGHT, 153.0, country="AU", fid=none))
        self.assertEqual([r[0].get("foot") for r in rows], [None, None], "the kerb and the grass, no footpath")
        self.assertEqual(len(roads.au_verge(RES, "AU", None)["foot"]), 2, "no id: both")
        # only residential / living streets lose footpaths; the other minor classes keep them
        self.assertEqual({roads.au_verge(S(6.0, "paved", True, "unclassified", False), "AU", f"w{i}")["foot"]
                          for i in range(60)}, {(1, -1)})
        self.assertIsNone(roads.au_verge(S(6.0, "paved", False, "residential", False), "AU", "x"), "rural: no sidewalks")

class VergeTileTest(unittest.TestCase):
    def tile(self, country="AU", tag="both", **kw):
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [grid(tag)], flat, URBAN, walks=walks, country=country, **kw)
        return walks, chunks

    def test_the_tile_has_kerb_runs_and_footpath_runs(self):
        walks, _ = self.tile()
        runs = [w for w in walks if "pts" in w]
        kerb = [w for w in runs if not w.get("foot")]
        foot = [w for w in runs if w.get("foot")]
        self.assertTrue(kerb and foot)
        self.assertTrue(all(not w["ramps"] for w in foot), "no kerb ramps on a footpath")
        self.assertTrue(any(w["ramps"] for w in kerb), "the kerb keeps its ramps at the crosswalks")
        corners = [w["corner"] for w in walks if "corner" in w]
        self.assertTrue(corners and all(c.get("h") == roads.AU_KERB_H for c in corners), "the curb returns: the low kerb")
        us, _ = self.tile("US")
        self.assertFalse(any(w.get("foot") or "h" in w or "strip" in w for w in us if "pts" in w), "elsewhere: as before")
        self.assertFalse(any("h" in w["corner"] for w in us if "corner" in w))

    def test_furniture_and_parking_stand_on_the_kerb_run_not_the_footpath(self):
        props, park = [], []
        walks, _ = self.tile(props=props, park=park, town=True)
        edge = next(w["pts"][0, 1] for w in walks if w.get("fid") == "id000" and not w.get("foot") and w["side"] == 1
                    and "pts" in w) - 100 - roads.AU_KERB_W / 2
        north = [p for p in props if p[2] > 100 and abs(p[1] - 120) > 15 and p[1] < 250]
        poles = [p for p in north if p[0] == furniture.G_POLE]
        trees = [p for p in north if p[0] == furniture.G_STREET_TREE]
        self.assertTrue(poles and trees)
        for p in poles + trees:
            self.assertTrue(0.3 < p[2] - 100 - edge < 2.0, "in the grass strip behind the kerb")
        for p in props:         # nothing stands in the footpath's band
            if p[0] in (furniture.G_POLE, furniture.G_STREET_TREE, furniture.G_LAMP) and p[1] < 100 + 1:
                self.assertFalse(edge + 2.4 < abs(p[2] - 100) < edge + 4.0, p)
        self.assertTrue(park and all(abs(s[1] - 100) < edge for s in park if abs(s[0] - 120) > 15 and abs(s[1] - 100) < 30),
                        "parked cars at the kerb")


class VergePaintTest(unittest.TestCase):
    """The ground: lawn in the strip, asphalt under the footpath (Brisbane's lat / lon, where the strip is drawn)."""
    LAT0, LON0 = -27.5196, 152.9788

    def grid(self, lat0, lon0, n=96, sq=1.0):
        m_lat, m_lon = 111320.0, 111320.0 * np.cos(np.radians(lat0))
        rows, cols = np.arange(n)[:, None], np.arange(n)[None, :]
        return lat0 + (n / 2 - rows) * sq / m_lat + np.zeros((1, n)), lon0 + (cols - n / 2) * sq / m_lon + np.zeros((n, 1))

    def street(self, lat0, lon0, tag="both", fid="id000"):
        m_lat, m_lon = 111320.0, 111320.0 * np.cos(np.radians(lat0))
        f = features([[(-200, 0), (200, 0)]], ["residential"], ids=[fid])
        f.lonlat[:] = np.array([(lon0 + e / m_lon, lat0) for e in (-200.0, 200.0)])
        if tag:
            f.attrs["osm_sidewalk"] = np.array([tag])
        return f

    def north(self, lat0, lon0, tag="both", fid="id000"):
        """The ground metre by metre north of the road's middle (the input ground is dirt, so 'dirt' is untouched)."""
        lat, lon = self.grid(lat0, lon0)
        out = sf.paint_roads(np.full(lat.shape, sf.DIRT, np.uint8), lat, lon, [self.street(lat0, lon0, tag, fid)], 1.0, URBAN)
        return [int(v) for v in out[:48, 10]][::-1]

    def test_a_brisbane_street_has_lawn_in_its_strip_and_asphalt_under_its_footpath(self):
        n = self.north(self.LAT0, self.LON0)
        self.assertTrue(all(v == sf.ASPHALT for v in n[:5]), "the carriageway and its kerb")
        self.assertEqual(n[5:7], [sf.LAWN, sf.LAWN], "the strip: lawn, so the 3D grass grows there")
        self.assertEqual(n[7], sf.ASPHALT, "the footpath's ground is asphalt: no grass through the slab")
        self.assertEqual(n[9], sf.DIRT, "beyond the corridor, the ground as classified")

    def test_other_countries_keep_their_asphalt_under_the_sidewalk(self):
        n = self.north(39.2834, -76.5934)
        self.assertTrue(all(v == sf.ASPHALT for v in n[:8]), "the 2.5 m sidewalk's ground, as before")
        self.assertNotIn(sf.LAWN, n)


if __name__ == "__main__":
    unittest.main()
