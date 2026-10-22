"""The Netherlands' road surfaces from the BGT (bgt.py, --nl-bgt on; Rotterdam, 2026-10-21)."""
import gzip
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bgt  # noqa: E402
import geodesy  # noqa: E402

FRAME = geodesy.EnuFrame(51.9075, 4.4668)


def ring(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float),
                                      np.zeros(len(pts)))
    return np.c_[lo, la]


def square(x0, y0, x1, y1):
    return ring([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])


def poly(rings, functie="rijbaan lokale weg", voorkomen="gesloten verharding", level=0, pid="a"):
    return bgt.Poly(pid, "wegdeel", functie, voorkomen, level, rings)


class LookTest(unittest.TestCase):
    def test_by_function_and_surface(self):
        self.assertEqual(bgt.look("rijbaan lokale weg", "gesloten verharding"), bgt.ASPHALT)
        self.assertEqual(bgt.look("rijbaan lokale weg", "open verharding"), bgt.STONE)
        self.assertEqual(bgt.look("parkeervlak", "open verharding"), bgt.STONE)
        self.assertEqual(bgt.look("fietspad", "gesloten verharding"), bgt.RED)
        self.assertEqual(bgt.look("voetpad", "open verharding"), bgt.TILES)
        self.assertEqual(bgt.look("verkeerseiland", "open verharding"), bgt.TILES)
        self.assertEqual(bgt.look("OV-baan", "half verhard"), bgt.GRAVEL)
        self.assertIsNone(bgt.look("berm", "groenvoorziening"), "a verge: the photo")
        self.assertIsNone(bgt.look("rijbaan lokale weg", "onverhard"))


def _vint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _fld(f, w, payload):
    if w == 0:
        return _vint(f << 3) + _vint(payload)
    return _vint((f << 3) | 2) + _vint(len(payload)) + payload


def _zz(v):
    return (v << 1) ^ (v >> 63)


def mvt_tile(layers):
    """A minimal MVT tile: {layer name: [(type, props, parts)]}, parts [[(x, y), ...]] in tile units (extent 4096)."""
    out = b""
    for name, feats in layers.items():
        keys, vals, body = [], [], b""
        for gtype, props, parts in feats:
            tags = []
            for k, v in props.items():
                if k not in keys:
                    keys.append(k)
                if v not in vals:
                    vals.append(v)
                tags += [keys.index(k), vals.index(v)]
            cmds, cx, cy = [], 0, 0
            for part in parts:
                pts = part[:-1] if gtype == 3 else part
                cmds += [(1 << 3) | 1, _zz(pts[0][0] - cx), _zz(pts[0][1] - cy)]
                cx, cy = pts[0]
                if len(pts) > 1:
                    cmds.append(((len(pts) - 1) << 3) | 2)
                    for x, y in pts[1:]:
                        cmds += [_zz(x - cx), _zz(y - cy)]
                        cx, cy = x, y
                if gtype == 3:
                    cmds.append((1 << 3) | 7)
            body += _fld(2, 2, _fld(2, 2, b"".join(_vint(t) for t in tags)) + _fld(3, 0, gtype)
                         + _fld(4, 2, b"".join(_vint(c) for c in cmds)))
        lb = _fld(15, 0, 2) + _fld(1, 2, name.encode()) + body
        lb += b"".join(_fld(3, 2, k.encode()) for k in keys)
        lb += b"".join(_fld(4, 2, _fld(1, 2, v.encode()) if isinstance(v, str) else _fld(5, 0, v & ((1 << 64) - 1)))
                       for v in vals)
        lb += _fld(5, 0, 4096)
        out += _fld(3, 2, lb)
    return out


# an outer ring of positive area in tile coordinates (y down), its hole the other way round
SQ = [(1000, 1000), (3000, 1000), (3000, 3000), (1000, 3000), (1000, 1000)]
HOLE = [(1500, 1500), (1500, 2500), (2500, 2500), (2500, 1500), (1500, 1500)]


class TilePolysTest(unittest.TestCase):
    def test_areas_holes_lines_points_levels(self):
        data = mvt_tile({
            "wegdeel": [(3, {"lokaal_id": "W", "functie": "parkeervlak", "fysiek_voorkomen": "open verharding",
                             "relatieve_hoogteligging": -1}, [SQ, HOLE])],
            "scheiding": [(2, {"lokaal_id": "S", "type": "muur"}, [[(100, 100), (900, 100)]])],
            "paal": [(1, {"lokaal_id": "P", "bgt_type": "niet-bgt", "plus_type": "lichtmast"}, [[(50, 60)]])],
            "pand": [(3, {"lokaal_id": "B"}, [SQ])]})
        polys = bgt.tile_polys(data, 17, 67190, 43374, ("wegdeel", "scheiding", "paal"))
        by = {p.layer: p for p in polys}
        self.assertEqual(sorted(by), ["paal", "scheiding", "wegdeel"], "only the layers asked for")
        w = by["wegdeel"]
        self.assertEqual((w.functie, w.voorkomen, w.level), ("parkeervlak", "open verharding", -1),
                         "a -1 level written as an unsigned int64")
        self.assertEqual(len(w.rings), 2, "the outer ring and its hole")
        self.assertEqual(by["scheiding"].functie, "muur")
        self.assertEqual(by["paal"].functie, "lichtmast")
        lon0, lat0, lon1, lat1 = w.own
        self.assertTrue(lon0 < w.rings[0][:, 0].mean() < lon1 and lat0 < w.rings[0][:, 1].mean() < lat1)


class TileCacheTest(unittest.TestCase):
    def test_fetched_once_kept_on_disk(self):
        from types import SimpleNamespace
        calls = []

        class F:
            def get(self, url, kind="demand"):
                calls.append(url)
                return SimpleNamespace(body=gzip.compress(mvt_tile({"wegdeel": [(3, {"lokaal_id": "W"}, [SQ])]})))
        with tempfile.TemporaryDirectory() as d:
            first = bgt.Bgt(d, F()).tile(67190, 43374)
            self.assertTrue(first and first[:2] != b"\x1f\x8b", "kept unzipped")
            self.assertEqual(bgt.Bgt(d, F()).tile(67190, 43374), first, "another process: from disk")
            self.assertEqual(len(calls), 1)


class MeshTest(unittest.TestCase):
    def test_flat_roads_render_kerbed_islands_collide(self):
        box = (-50.0, -50.0, 50.0, 50.0)
        road = poly([square(-20, -5, 20, 5), square(-2, -2, 2, 2)], pid="road")          # a 4 m hole
        island = poly([square(-20, 10, 20, 12)], functie="verkeerseiland", voorkomen="open verharding", pid="isl")
        verge = poly([square(-20, 20, 20, 30)], functie="berm", voorkomen="groenvoorziening", pid="berm")
        ground = lambda la, lo: np.full(np.shape(la), 3.0)             # noqa: E731
        solid = []
        m = bgt.mesh(FRAME, box, [road, island, verge], ground, ground, (0.0, 0.0, 0.0), 0.0, solid=solid)
        V, T, UV, mats, idx = m
        self.assertEqual(mats, [bgt.ASPHALT[0]], "the road renders; the verge is left to the photo")
        c = V[T].mean(1)
        self.assertFalse(((np.abs(c[:, 0]) < 1.5) & (np.abs(c[:, 1]) < 1.5)).any(), "the hole is cut")
        z_road = V[:, 2].mean()
        self.assertGreater(z_road, 2.9)
        self.assertEqual(len(solid), 1)
        S, ST, _, smats, sidx = solid[0]
        self.assertEqual(set(smats), {bgt.TILES[0], bgt.KERB}, "the island's top and its kerb faces collide")
        top = S[ST[sidx == smats.index(bgt.TILES[0])]][:, :, 2].mean()
        self.assertAlmostEqual(top - z_road, bgt.KERB_M, places=2)
        kerb = S[ST[sidx == smats.index(bgt.KERB)]]
        self.assertAlmostEqual(kerb[:, :, 2].max() - kerb[:, :, 2].min(), bgt.KERB_M + 0.005, places=3)

    def test_no_kerb_between_two_footways(self):
        a = poly([square(-10, 0, 0, 3)], functie="voetpad", voorkomen="open verharding", pid="a")
        b = poly([square(0, 0, 10, 3)], functie="voetpad", voorkomen="open verharding", pid="b")
        ground = lambda la, lo: np.zeros(np.shape(la))                 # noqa: E731
        solid = []
        bgt.mesh(FRAME, (-50, -50, 50, 50), [a, b], ground, ground, (0.0, 0.0, 0.0), 0.0, solid=solid)
        S, ST, _, smats, sidx = solid[0]
        faces = S[ST[sidx == smats.index(bgt.KERB)]].mean(1)
        self.assertFalse((np.abs(faces[:, 0]) < 0.5).any(), "no kerb face where they meet (x = 0)")
        self.assertTrue((np.abs(faces[:, 1]) < 0.5).any(), "but along the road side (y = 0)")

    def test_street_over_a_trench_on_the_ground_and_colliding(self):
        street = poly([square(-5, -5, 5, 5)], level=0, pid="street")
        deep = poly([square(20, -5, 30, 5)], level=-1, pid="deep")
        graded = lambda la, lo: np.full(np.shape(la), -4.0)           # noqa: E731  the trench graded under it
        raw = lambda la, lo: np.full(np.shape(la), 1.0)               # noqa: E731
        solid = []
        m = bgt.mesh(FRAME, (-50, -50, 50, 50), [street, deep], graded, raw, (0.0, 0.0, 0.0), 0.0, solid=solid)
        self.assertLess(m[0][:, 2].max(), -3.5, "the sunken road on the graded ground, render only")
        self.assertGreater(solid[0][0][:, 2].min(), 0.5, "the street over it on the ground, colliding")


class PassageTest(unittest.TestCase):
    """2026-10-21: 's-Gravendijkwal under the Droogleever Fortuynplein: the DEM has the junction's deck where the road
    goes under it, so the trench stopped in a mound at each side of the junction."""

    def setUp(self):
        # a trench along y (floor -1.3) under a 20 m deck across it (y -10 .. 10, ground 3.2 in the DEM)
        self.under = poly([square(-6, -10, 6, 10)], functie="", voorkomen="", level=-1, pid="tun")
        self.under.layer = bgt.PASSAGE

    @staticmethod
    def dem(la, lo):
        e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
        e, n = np.asarray(e), np.asarray(n)
        trench = np.abs(e) < 6
        return np.where(trench & (np.abs(n) >= 14), -1.3, np.where(trench & (np.abs(n) >= 10), 1.0, 3.2))  # (blur)

    def test_terrain_dug_to_the_floor_under_the_deck(self):
        passes = bgt.passages(FRAME, [self.under, poly([square(-6, -10, 6, 10)], level=-1, pid="road")])
        # (the tunneldeel only: a road under the deck that climbs to the street was dug flat to the floor all along,
        # pits north of 's-Gravendijkwal's crossing, 2026-10-21)
        self.assertEqual([p.id for p in passes], ["tun"])
        dug = bgt.cut(FRAME, passes, self.dem)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.0, 3.0, 2.0, 20.0, 0.0]),
                                          np.array([0.0, 8.0, -9.0, -12.0, 0.0, 30.0]), np.zeros(6))
        z = dug(la, lo)
        np.testing.assert_allclose(z[:4], -1.3, atol=0.05, err_msg="under the deck and the DEM's blur past its end")
        self.assertAlmostEqual(float(z[4]), 3.2, msg="beside it the ground as it was")
        self.assertAlmostEqual(float(z[5]), -1.3, msg="the trench as it was")
        self.assertIs(bgt.cut(FRAME, [], self.dem), self.dem)

    def test_street_over_it_is_a_level_colliding_deck(self):
        passes = bgt.passages(FRAME, [self.under])
        dug = bgt.cut(FRAME, passes, self.dem)
        street = poly([square(-60, -8, 60, 8)], level=0, pid="street")
        def blur(la, lo):           # the DEM's deck sagging into the trench over its last 6 m (its 6 m pixels)
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            sag = 4.5 * np.clip(1 - (10 - np.abs(np.asarray(n))) / 6, 0, 1) * (np.abs(np.asarray(e)) < 6)
            return np.minimum(self.dem(la, lo), 3.2 - sag)
        solid = []
        flat = bgt.mesh(FRAME, (-80, -80, 80, 80), [street], dug, blur, (0.0, 0.0, 0.0), 0.0, solid=solid,
                        passes=passes)
        S = solid[0][0]
        self.assertTrue((np.abs(S[:, 0]) < 7).any(), "the street over the passage collides")
        np.testing.assert_allclose(S[:, 2], 3.2 + 0.005, atol=0.02)
        self.assertLess(np.abs(S[:, 0]).max(), 30, "only by the passage: not all of the street a deck")
        self.assertGreater(np.abs(flat[0][:, 0]).max(), 50, "the rest of it on the ground")


    def test_trench_road_past_its_end_stays_on_the_floor(self):
        passes = bgt.passages(FRAME, [self.under])
        dug = bgt.cut(FRAME, passes, self.dem)
        road = poly([square(-5, 10, 5, 30)], level=0, pid="trench")     # BGT: the open trench's road is level 0
        flat = bgt.mesh(FRAME, (-50, -50, 50, 50), [road], dug, self.dem, (0.0, 0.0, 0.0), 0.0, solid=[],
                        passes=passes)
        self.assertLess(flat[0][:, 2].max(), -1.2, "no step up in the DEM's blur of the deck's edge")


    def test_trench_road_up_its_ramp_is_still_the_trenchs(self):
        def ramp(la, lo):           # the trench's floor to 40 m out, then its ramp up to the street at 90 m
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            n = np.abs(np.asarray(n))
            up = np.clip((n - 40) / 50, 0, 1) * 4.5 - 1.3
            return np.where((np.abs(np.asarray(e)) < 6) & (n >= 10), np.where(n < 14, 1.0, up), 3.2)
        passes = bgt.passages(FRAME, [self.under])
        dug = bgt.cut(FRAME, passes, ramp)
        road = poly([square(-5, 10, 5, 90)], level=0, pid="trench")
        flat = bgt.mesh(FRAME, (-100, -100, 100, 100), [road], dug, ramp, (0.0, 0.0, 0.0), 0.0, solid=[],
                        passes=passes)
        V = flat[0]
        self.assertLess(V[V[:, 1] < 30][:, 2].max(), -1.2, "no slab over the floor before the deck")

    def test_wall_over_its_mouth_is_the_decks_edge(self):
        passes = bgt.passages(FRAME, [self.under])
        dug = bgt.cut(FRAME, passes, self.dem)
        wall = bgt.Poly("w", bgt.WALLS, "muur", "", 0, [ring([(-12, 10.3), (12, 10.3)])])
        w = bgt.walls(FRAME, (-50, -50, 50, 50), [wall], dug, self.dem, passes=passes)
        self.assertEqual(len(w), 1)
        mid = np.abs(w[0]["base"][:, 0]) < 3
        self.assertTrue(mid.any())
        self.assertLessEqual((w[0]["top"] - w[0]["foot"])[mid].max(), bgt.FASCIA_M + 1e-6, "the mouth stays open")
        self.assertTrue((w[0]["foot"][mid] > 0.5).all())


    def test_the_maps_tunnel_through_it_is_not_drawn(self):
        under = self.under

        class Fake:
            def passages(self, frame, box):
                return bgt.passages(frame, [under])
        skip = bgt.skips_tunnel(Fake(), FRAME, self.dem)
        self.assertTrue(skip(np.array([[0.0, -16.0], [0.0, 16.0]])), "the tunnel under the junction: the BGT's")
        self.assertFalse(skip(np.array([[40.0, -16.0], [40.0, 16.0]])), "one elsewhere stays a tunnel")


    def test_grading_does_not_cut_into_the_banks(self):
        graded = lambda la, lo: np.full(np.shape(la), -0.6)            # noqa: E731  the trench's slopes cut wide
        passes = bgt.passages(FRAME, [self.under])
        kept = bgt.banks(FRAME, passes, graded, self.dem)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([11.5, 0.0, 0.0]), np.array([0.0, 30.0, 0.0]), np.zeros(3))
        z = kept(la, lo)
        self.assertAlmostEqual(float(z[0]), 3.2 - bgt.CUT_KEEP_M, msg="the bank beside the trench: (about) as it is")
        self.assertAlmostEqual(float(z[1]), -0.6, msg="the trench: as graded")
        self.assertAlmostEqual(float(z[2]), -0.6, msg="under the deck: the passage's")


class WallsAndLampsTest(unittest.TestCase):
    def test_wall_between_a_trench_and_the_street(self):
        wall = poly([square(-20, -0.2, 20, 0.2)], functie="muur", pid="w")
        fence = poly([square(-20, 5, 20, 5.3)], functie="hek", pid="h")
        graded = lambda la, lo: np.where(FRAME.geodetic_to_enu(la, lo, 0.0)[1] < 0, -4.0, 1.0)   # noqa: E731
        raw = lambda la, lo: np.full(np.shape(la), 1.0)                                          # noqa: E731
        out = bgt.walls(FRAME, (-50, -50, 50, 50), [wall, fence], graded, raw)
        self.assertEqual(len(out), 1, "a fence is no wall")
        w = out[0]
        self.assertLess(w["toward"][:, 1].mean(), -0.9, "faces the trench (south)")
        self.assertLess(w["foot"].max(), -3.5)
        self.assertGreater(w["top"].min(), 0.9)

    def test_street_lights_turned_to_the_road(self):
        pole = poly([np.array([ring([(0.0, 6.0)])[0]])], functie="lichtmast", pid="p")
        road = poly([square(-20, -3, 20, 3)], pid="r")
        rows = bgt.lamps(FRAME, (-50, -50, 50, 50), [pole], [road], lambda la, lo: np.zeros(np.shape(la)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "streetlight")
        self.assertAlmostEqual(rows[0][2], 6.0, places=1)


class RailsTest(unittest.TestCase):
    def test_two_rails_a_gauge_apart_trams_only(self):
        tram = poly([ring([(-20.0, 0.0), (20.0, 0.0)])], functie="tram", pid="t")
        train = poly([ring([(-20.0, 10.0), (20.0, 10.0)])], functie="trein", pid="r")
        m = bgt.rails_mesh(FRAME, (-50, -50, 50, 50), [tram, train], lambda la, lo: np.zeros(np.shape(la)),
                           (0.0, 0.0, 0.0), 0.0)
        V, T = m[0], m[1]
        ys = np.unique(np.round(V[T].mean(1)[:, 1], 2))
        self.assertTrue(np.allclose(sorted(set(np.round(ys, 1))), [-0.7, 0.7], atol=0.05), ys)
        self.assertTrue((np.abs(V[:, 1]) < 1.0).all(), "the train track is not a tram's")


class FilterTest(unittest.TestCase):
    def test_markings_decks_and_tunnels_kept(self):
        chunks = [{"m": "road_asphalt_2lane", "p": 20}, {"m": "sidewalk_concrete", "p": 16},
                  {"m": "line_white", "p": 2}, {"m": "road_asphalt_2lane", "p": 20, "deck": True},
                  {"m": "road_asphalt_2lane", "p": 20, "tunnel": True}]
        kept = bgt.keep_markings(chunks)
        self.assertEqual([c["m"] for c in kept], ["line_white", "road_asphalt_2lane", "road_asphalt_2lane"])

    def test_polygons_on_a_deck_or_in_a_tunnel_left_out(self):
        on_deck = poly([square(-3, -3, 3, 3)], level=1, pid="on")
        beside = poly([square(30, -3, 36, 3)], level=1, pid="off")
        ground = poly([square(-3, -3, 3, 3)], level=0, pid="ground")
        chunks = [{"deck": True, "w": 8.0, "n": [[-20.0, 0.0, 0.0], [20.0, 0.0, 0.0]]}]
        kept = bgt.off_structures(FRAME, [on_deck, beside, ground], chunks)
        self.assertEqual([p.id for p in kept], ["off", "ground"])


if __name__ == "__main__":
    unittest.main()
