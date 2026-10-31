"""townsigns: a commune's entry and exit signs where a paved road enters or leaves its agglomeration (its built-up land
as one area), the same places whichever road segment or tile draws them; their faces, and the road tile naming them."""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import revisit  # noqa: E402
import roads  # noqa: E402
import townsigns as ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

M = ts.M_PER_DEG


class Frame:
    """A frame at (0, 0): x metres east is lon x / M, y metres north lat y / M (the cos of latitude 0 is 1)."""

    def enu_to_geodetic(self, e, n, u=0.0):
        return np.asarray(n, np.float64) / M, np.asarray(e, np.float64) / M, np.zeros(np.shape(e))

    def geodetic_to_enu(self, lat, lon, h=0.0):
        return np.asarray(lon, np.float64) * M, np.asarray(lat, np.float64) * M, np.zeros(np.shape(lat))


FRAME = Frame()
BOX = (-1000.0, -1000.0, 1000.0, 1000.0)


def road(fid, cls="tertiary", x0=-500.0, x1=900.0, y=0.0, width=7.0, kind="paved", oneway=False, d=0, link=False):
    pts = np.array([(x0, y, 0.0), (x1, y, 0.0)])
    return (fid, roads.Spec(width, kind, False, cls, oneway, dir=d, link=link), pts, 0.0)


def commune(name, x0, y0, x1, y1, built):
    """A square commune; built(x, y) -> bool array: its built-up land, in frame metres."""
    xy = [[np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], np.float64)]]
    ll = [[r / M for r in rings] for rings in xy]
    c = ts.Commune(name, xy, ll, None)
    return c._replace(agglo=ts.agglomeration(ll, lambda lat, lon: built(np.asarray(lon) * M, np.asarray(lat) * M)))


def east_of(x0):
    return lambda x, y: np.asarray(x) >= x0


def signs(rs, communes, box=BOX, **kw):
    return ts.town_signs(rs, communes, FRAME, box, **kw)


class AgglomerationTest(unittest.TestCase):
    def test_entry_and_exit_where_the_buildings_begin(self):
        """A village whose houses begin 200 m inside the commune (the boundary at x = 0): its sign stands EDGE_M before
        the houses, not at the boundary, on the eastbound kerb (south) with the entry face toward the traffic coming in
        and the exit face on its back, toward the traffic going out (back to back, the user 2026-10-28)."""
        village = commune("SAINT-CHRISTOL-LEZ-ALES", 0, -1500, 2000, 1500, east_of(200.0))
        rows = signs([road("r")], [village])
        self.assertEqual(sorted(r[0] for r in rows), [ts.G_ENTRY, ts.G_EXIT])
        entry = next(r for r in rows if r[0] == ts.G_ENTRY)
        exit_ = next(r for r in rows if r[0] == ts.G_EXIT)
        for r in rows:
            self.assertAlmostEqual(r[1], 200.0 - ts.EDGE_M, delta=ts.AGGLO_CELL_M)
            self.assertEqual(r[8], "SAINT-CHRISTOL-LEZ-ALES")
        self.assertLess(entry[2], 0.0, "eastbound: its kerb is the south side")
        self.assertEqual(exit_[1:4], entry[1:4], "the exit face on the entry sign's back: one spot")
        self.assertAlmostEqual((exit_[4] - entry[4]) % 360.0, 180.0, places=6)
        self.assertAlmostEqual(abs(entry[2]), 7.0 / 2 + ts.SIGN_OFF_M, places=2)

    def test_two_towns_grown_together(self):
        """Built-up land all along, the boundary at x = 300: each kerb has the sign of the town its traffic enters,
        the other town's exit on its back (seen across the road), both at the boundary."""
        a = commune("ALES", -2000, -1500, 300, 1500, east_of(-1e9))
        b = commune("SAINT-MARTIN-DE-VALGALGUES", 300, -1500, 2000, 1500, east_of(-1e9))
        rows = signs([road("r")], [a, b])
        south = sorted((r[0], r[8]) for r in rows if r[2] < 0)      # the eastbound kerb: it enters Saint-Martin
        north = sorted((r[0], r[8]) for r in rows if r[2] > 0)
        self.assertEqual(south, [(ts.G_ENTRY, "SAINT-MARTIN-DE-VALGALGUES"), (ts.G_EXIT, "SAINT-MARTIN-DE-VALGALGUES")])
        self.assertEqual(north, [(ts.G_ENTRY, "ALES"), (ts.G_EXIT, "ALES")])
        for r in rows:
            self.assertAlmostEqual(r[1], 300.0, delta=ts.STEP_M / 2 ** ts.REFINE)

    def test_a_commune_without_built_land_has_its_signs_at_its_boundary(self):
        """A hamlet the land cover misses (no built-up land in or near the commune): its whole area counts."""
        hamlet = commune("LE MAS", 100, -500, 400, 500, east_of(1e9))
        self.assertIsNone(hamlet.agglo)
        rows = signs([road("r")], [hamlet])
        self.assertEqual(len(rows), 4)
        self.assertEqual(sorted({round(r[1]) for r in rows}), [100, 400])

    def test_villages_of_one_gemeinde_grown_together(self):
        """Two villages of one German Gemeinde (group) grown together: no sign between them, the town's signs at its
        ends with the name of the village there; two Gemeinden: their signs at the boundary."""
        def two(group_b):
            a = commune("Ortsteil A", 0, -1500, 1000, 1500, east_of(200.0))._replace(group="g", country="DE")
            b = commune("Ortsteil B", 1000, -1500, 3000, 1500, lambda x, y: np.asarray(x) < 1800.0)
            return [a, b._replace(group=group_b, country="DE")]
        rows = signs([road("r", x1=2500.0)], two("g"), box=(-3000, -3000, 3000, 3000))
        self.assertEqual(sorted({(round(r[1], -1), r[8]) for r in rows}),
                         [(160.0, "Ortsteil A"), (1840.0, "Ortsteil B")])
        rows = signs([road("r", x1=2500.0)], two("h"), box=(-3000, -3000, 3000, 3000))
        self.assertIn(1000.0, {round(r[1], -1) for r in rows}, "two Gemeinden: a sign at their boundary")

    def test_a_garden_does_not_end_the_town(self):
        """A 30 m gap in the built-up land (a garden, a small field) inside the village: no sign there."""
        village = commune("BAGARD", 0, -1500, 2000, 1500,
                          lambda x, y: (np.asarray(x) >= 200.0) & ~((np.asarray(x) > 500.0) & (np.asarray(x) < 530.0)))
        self.assertEqual(len(signs([road("r")], [village])), 2)

    def test_a_field_inside_the_town_is_in_it(self):
        """A 300 m field the town's streets go round (the land it encloses): no exit sign at its edge."""
        def built(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return (x >= 200.0) & ~((np.abs(x - 800.0) < 150.0) & (np.abs(y) < 150.0))
        village = commune("BOISSET-ET-GAUJAC", 0, -1500, 2000, 1500, built)
        self.assertEqual(len(signs([road("r", x1=1500.0)], [village])), 2)

    def test_a_farm_is_no_town(self):
        """A farm's 60 m of built-up land beside the road, far from the village: no signs there."""
        def built(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return (x >= 1200.0) | ((np.abs(x - 300.0) < 30.0) & (np.abs(y) < 30.0))
        village = commune("TORNAC", 0, -1500, 2000, 1500, built)
        rows = signs([road("r", x1=1500.0)], [village], box=(-2000, -2000, 2000, 2000))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(abs(r[1] - (1200.0 - ts.EDGE_M)) < ts.AGGLO_CELL_M for r in rows), rows)

    def test_a_main_roads_built_up_strip_is_not_the_town(self):
        """The land cover counts a main road's carriageway as built-up: a 40 m strip along the road out of the village
        for 1 km (Le Vernet's N20, 2026-10-29) does not carry the village with it; its sign stays at the houses."""
        def built(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return ((x >= 1000.0) & (x <= 1600.0)) | ((x >= 0.0) & (np.abs(y) < 20.0))
        village = commune("LE VERNET", -1500, -1500, 2000, 1500, built)
        rows = signs([road("r", x0=-1200.0, x1=1900.0)], [village], box=(-2000, -2000, 2000, 2000))
        xs = sorted({round(r[1]) for r in rows if r[2] < 0})
        self.assertTrue(xs and abs(xs[0] - (1000.0 - ts.EDGE_M)) <= ts.AGGLO_CELL_M + ts.STEP_M, xs)

    def test_a_village_all_thin_is_kept(self):
        """A village of a few houses along one lane, its built-up land all a strip the opening would take out
        (Luxembourg's Mamer and Wiltz raised on it, 2026-10-29): its strip is its agglomeration."""
        ll = [[np.array([(0.0, 0.0), (0.02, 0.0), (0.02, 0.02), (0.0, 0.02)])]]
        a = ts.agglomeration(ll, lambda lat, lon: (np.abs(np.asarray(lat) - 0.01) < 0.0001)
                             & (np.abs(np.asarray(lon) - 0.01) < 0.004))
        self.assertIsNotNone(a)
        self.assertTrue(a.mask.any())

    def test_a_village_too_small_keeps_its_largest_piece(self):
        """No piece of MIN_AGGLO_M2: the largest is the village (a small bourg under trees), the smaller one not."""
        def built(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return ((np.abs(x - 300.0) < 60.0) & (np.abs(y) < 60.0)) | ((np.abs(x - 700.0) < 30.0) & (np.abs(y) < 30.0))
        village = commune("GENERARGUES", 0, -1500, 2000, 1500, built)
        xs = sorted({round(r[1]) for r in signs([road("r")], [village])})
        self.assertEqual(len(xs), 2)
        self.assertTrue(all(abs(abs(x - 300) - 60 - ts.EDGE_M) <= ts.AGGLO_CELL_M + ts.STEP_M for x in xs), xs)

    def test_a_road_along_the_last_houses_does_not_flicker(self):
        """A road beside the village's last houses goes in and out of the area every 150 m for 600 m (Tornac's
        D982, 2026-10-28): the short stretches are taken out, one entry where the town really begins."""
        def built(x, y):
            x, y = np.asarray(x), np.asarray(y)
            teeth = (x >= 200.0) & ((x // 75.0) % 2 == 0) & (y < -20.0)       # houses south of the road, in groups
            return (x >= 800.0) | teeth | ((x >= 200.0) & (y < -60.0))
        village = commune("TORNAC", 0, -1500, 2000, 1500, built)
        rows = signs([road("r", x1=1500.0)], [village])
        self.assertEqual(len([r for r in rows if r[0] == ts.G_ENTRY]), 1, rows)

    def test_a_gap_between_a_villages_parts(self):
        """The road leaves the village's built-up land for 200 m (a field, a river bank) and comes back to the same
        village (Val-Cenis, 2026-10-28): no exit and entry there; a 600 m gap is two parts, each with its signs."""
        def village(gap):
            return commune("VAL-CENIS", 0, -1500, 3000, 1500,
                           lambda x, y: (np.asarray(x) >= 200.0)
                           & ~((np.asarray(x) > 600.0) & (np.asarray(x) < 600 + gap)))
        self.assertEqual(len(signs([road("r", x1=1500.0)], [village(200.0)], box=(-2000, -2000, 2000, 2000))), 2)
        self.assertEqual(len(signs([road("r", x1=1500.0)], [village(600.0)], box=(-2000, -2000, 2000, 2000))), 6)

    def test_outside_every_commune_nothing(self):
        self.assertEqual(signs([road("r")], []), [])
        far = commune("LOIN", 5000, 5000, 6000, 6000, east_of(0.0))
        self.assertEqual(signs([road("r")], [far]), [])


class RoadsTest(unittest.TestCase):
    VILLAGE = None

    @classmethod
    def setUpClass(cls):
        cls.VILLAGE = [commune("BOISSET-ET-GAUJAC", 0, -1500, 2000, 1500, east_of(200.0))]

    def test_only_paved_roads_into_town(self):
        """No signs on motorways, trunks, links, service roads or tracks (France posts none there)."""
        for r in (road("m", "motorway"), road("t", "trunk"), road("s", "service"), road("k", "track"),
                  road("l", "secondary", link=True), road("g", "tertiary", kind="gravel")):
            self.assertEqual(signs([r], self.VILLAGE), [], r[1])
        for cls in ts.SIGN_CLASSES:
            self.assertEqual(len(signs([road("c", cls)], self.VILLAGE)), 2, cls)

    def test_no_sign_on_a_roundabouts_ring(self):
        """A roundabout's ring crossing into the village (rings: its id) has no sign; the road coming in has."""
        self.assertEqual(signs([road("ring")], self.VILLAGE, rings={"ring"}), [])
        self.assertEqual(len(signs([road("ring"), road("r", y=50.0)], self.VILLAGE, rings={"ring"})), 2)

    def test_one_way_road(self):
        """A one-way road into the village: the sign on its kerb (south, eastbound), its entry face toward the
        traffic. One out of it (westbound): the sign on the far kerb (south: the eastbound kerb, the way that does not
        exist), so its back, the exit face, is what the traffic sees across the road."""
        into = signs([road("o", oneway=True, d=1)], self.VILLAGE)
        out = signs([road("o", oneway=True, d=-1)], self.VILLAGE)
        for rows in (into, out):
            self.assertEqual(sorted(r[0] for r in rows), [ts.G_ENTRY, ts.G_EXIT])
            self.assertTrue(all(r[2] < 0 for r in rows))

    def test_left_hand_traffic_the_other_kerb(self):
        right = {r[0]: r for r in signs([road("r")], self.VILLAGE)}
        left = {r[0]: r for r in signs([road("r")], self.VILLAGE, left_hand=True)}
        self.assertLess(right[ts.G_ENTRY][2], 0.0)
        self.assertGreater(left[ts.G_ENTRY][2], 0.0)

    def test_a_road_in_two_segments_has_each_sign_once(self):
        """The road split where the town begins (Overture splits segments at their connectors): the signs of the two
        segments are the whole road's, each once."""
        whole = signs([road("r")], self.VILLAGE)
        parts = signs([road("a", x1=195.0)], self.VILLAGE) + signs([road("b", x0=195.0)], self.VILLAGE)
        self.assertEqual(sorted((r[0], r[1], r[2]) for r in parts), sorted((r[0], r[1], r[2]) for r in whole))

    def test_the_same_signs_whichever_tile_draws_them(self):
        r = road("r")
        whole = signs([r], self.VILLAGE)
        tiles = (signs([r], self.VILLAGE, box=(-1000, -1000, 175, 1000))
                 + signs([r], self.VILLAGE, box=(175, -1000, 1000, 1000)))
        self.assertEqual(sorted(map(tuple, tiles)), sorted(map(tuple, whole)))
        self.assertTrue(tiles)

    def test_signs_face_their_traffic(self):
        """The sign's yaw turns its local +x toward its kerb side (the speed-limit signs' convention): eastbound
        traffic's sign on the south kerb faces 270, westbound traffic's on the north kerb 90."""
        rows = {r[0]: r for r in signs([road("r")], self.VILLAGE)}
        self.assertAlmostEqual(rows[ts.G_ENTRY][4], 270.0, places=1)
        self.assertAlmostEqual(rows[ts.G_EXIT][4], 90.0, places=1)


class PiecesTest(unittest.TestCase):
    def test_the_same_pieces_as_a_cell_by_cell_walk(self):
        """_pieces (runs joined by a union-find) labels as a flood fill does: 8-connected, numbered in scan order."""
        from collections import deque

        def walk(m):
            lab, sizes, (h, w) = np.zeros(m.shape, np.int32), [0], m.shape
            for i0, j0 in zip(*np.nonzero(m)):
                if lab[i0, j0]:
                    continue
                n = len(sizes)
                lab[i0, j0], count, todo = n, 0, deque([(i0, j0)])
                while todo:
                    i, j = todo.popleft()
                    count += 1
                    for a in (i - 1, i, i + 1):
                        for b in (j - 1, j, j + 1):
                            if 0 <= a < h and 0 <= b < w and m[a, b] and not lab[a, b]:
                                lab[a, b] = n
                                todo.append((a, b))
                sizes.append(count)
            return lab, np.array(sizes)
        rng = np.random.default_rng(7)
        for density in (0.2, 0.45, 0.6):
            m = rng.random((60, 80)) < density
            lab, sizes = ts._pieces(m)
            want_lab, want_sizes = walk(m)
            self.assertTrue(np.array_equal(lab, want_lab), density)
            self.assertEqual(sizes.tolist(), want_sizes.tolist())
        self.assertEqual(ts._pieces(np.zeros((3, 3), bool))[1].tolist(), [0])


class CommuneTest(unittest.TestCase):
    def test_inside_leaves_the_holes_out(self):
        parts = [[np.array([(0, 0), (100, 0), (100, 100), (0, 100)], np.float64),
                  np.array([(40, 40), (60, 40), (60, 60), (40, 60)], np.float64)]]
        self.assertEqual(ts.inside(parts, [(10, 10), (50, 50), (150, 50)]).tolist(), [True, False, False])

    def test_the_grid_is_laid_on_latitude_and_longitude(self):
        """The same commune seen from two frames has the same agglomeration grid (the grid is not the frame's)."""
        ll = [[np.array([(0.001, 0.001), (0.01, 0.001), (0.01, 0.01), (0.001, 0.01)])]]
        a = ts.agglomeration(ll, lambda lat, lon: np.asarray(lon) > 0.005)
        b = ts.agglomeration([[r.copy() for r in rings] for rings in ll], lambda lat, lon: np.asarray(lon) > 0.005)
        self.assertEqual((a.lat0, a.lon0, a.dlat, a.dlon), (b.lat0, b.lon0, b.dlat, b.dlon))
        self.assertTrue(np.array_equal(a.mask, b.mask))
        self.assertAlmostEqual(a.lat0 / a.dlat, round(a.lat0 / a.dlat), places=6)

    def test_communes_of_each_id_once_in_sign_countries(self):
        """The divisions layer's features of several cells: each commune once (a commune is listed in every cell it
        touches), only where SIGN_COUNTRIES' rules apply, names with their spaces tidied."""
        class Feats:
            def __init__(self, rows):
                self.rows = rows
                self.ids = np.array([r[0] for r in rows])
                self.attrs = {"country": np.array([r[1] for r in rows]), "name": np.array([r[2] for r in rows])}

            def __len__(self):
                return len(self.rows)

            def parts(self, i):
                x0 = self.rows[i][3]
                return [[np.array([(x0, 0.0), (x0 + 0.01, 0.0), (x0 + 0.01, 0.01), (x0, 0.01)])]]

        a = Feats([("d-2", "FR", "Bagard", 0.0), ("d-1", "FR", "Saint-Christol-lez-Alès  ", 0.01)])
        b = Feats([("d-1", "FR", "Saint-Christol-lez-Alès", 0.01), ("d-3", "AQ", "McMurdo", 0.02)])
        out = ts.communes_of([a, None, b], FRAME)
        self.assertEqual([c.name for c in out], ["Saint-Christol-lez-Alès", "Bagard"])
        self.assertTrue(all(c.agglo is None for c in out))
        self.assertAlmostEqual(float(out[0].parts[0][0][:, 0].min()), 0.01 * M)
        self.assertAlmostEqual(float(out[0].lonlat[0][0][:, 0].min()), 0.01)
        self.assertEqual(out[0].fid, "d-1")
        self.assertEqual({c.local for c in out}, {""}, "no county: no regional name")
        self.assertEqual({c.country for c in out}, {"FR"})
        be = ts.communes_of([b], FRAME, country_ok={"BE", "AQ"})
        self.assertEqual([(c.name, c.country) for c in be], [("McMurdo", "AQ")], "each its own country")


class Divisions:
    """A divisions layer's features (mapdata's Features as townsigns reads them): rows (id, subtype, name, lon/lat
    ring, {language: name})."""

    def __init__(self, rows):
        self.rows = rows
        self.ids = np.array([r[0] for r in rows])
        self.attrs = {"country": np.array(["FR"] * len(rows)), "subtype": np.array([r[1] for r in rows]),
                      "name": np.array([r[2] for r in rows])}
        for k in ts.REGIONAL_LANGS:
            self.attrs[f"name_{k}"] = np.array([r[4].get(k, "") for r in rows])

    def __len__(self):
        return len(self.rows)

    def parts(self, i):
        return [[np.asarray(self.rows[i][3], np.float64)]]


def square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


class VillageTest(unittest.TestCase):
    """Luxembourg's signs name the villages of a commune (Overture county), points only in the data: each village has
    the part of its commune nearer its point than the others' (split_villages, nearest_site)."""

    class Points:
        def __init__(self, rows):          # (id, name, lb, lon, lat)
            self.rows = rows
            self.ids = np.array([r[0] for r in rows])
            self.attrs = {"country": np.array(["LU"] * len(rows)), "name": np.array([r[1] for r in rows]),
                          "name_lb": np.array([r[2] for r in rows])}

        def __len__(self):
            return len(self.rows)

        def parts(self, i):
            return [[np.array([[self.rows[i][3], self.rows[i][4]]])]]

    def test_two_villages_of_a_commune(self):
        """Hesperange's commune: its villages Hesperange (west) and Howald (east), each with its Luxembourgish name;
        a point is in the village it is nearer; a village outside the commune is left out; the same villages
        whichever order the cells come in."""
        xy = [[np.array([(0, 0), (2000, 0), (2000, 1000), (0, 1000)], np.float64)]]
        ll = [[r / M for r in rings] for rings in xy]
        com = ts.Commune("Hesperange", xy, ll, None, "c-1", "", "LU")
        pts = self.Points([("p-2", "Howald", "Houwald", 1500 / M, 500 / M), ("p-1", "Hesperange", "Hesper", 500 / M,
                           500 / M), ("p-3", "Itzig", "Izeg", 5000 / M, 500 / M)])
        v = ts.split_villages([com, ts.Commune("Ouest", xy, ll, None, "a-0")], lambda c: [pts])
        self.assertEqual([(c.name, c.local) for c in v],
                         [("Ouest", ""), ("Hesperange", "Hesper"), ("Howald", "Houwald")])
        again = ts.split_villages([com], lambda c: [self.Points(list(reversed(pts.rows)))])
        self.assertEqual([c.fid for c in again], [c.fid for c in v[1:]])
        st = ts.state([(400.0, 500.0), (1600.0, 500.0)], v[1:], FRAME)
        self.assertEqual(st.tolist(), [0, 1])

    def test_a_village_without_built_land_has_no_signs(self):
        """A hamlet's point in the fields: no built-up land in its part, no signs (not its part's edges)."""
        xy = [[np.array([(0, 0), (2000, 0), (2000, 1000), (0, 1000)], np.float64)]]
        ll = [[r / M for r in rings] for rings in xy]
        com = ts.Commune("Hesperange", xy, ll, None, "c-1", "", "LU")
        pts = self.Points([("p-1", "Hesperange", "Hesper", 500 / M, 500 / M), ("p-2", "Feld", "", 1500 / M, 500 / M)])
        v = ts.split_villages([com], lambda c: [pts])
        kept = ts.with_agglomerations(v, lambda c: ts.commune_agglomeration(
            c, lambda lat, lon: np.asarray(lon) * M < 900.0))
        self.assertEqual([c.name for c in kept], ["Hesperange"])


class SwissTest(unittest.TestCase):
    """Switzerland's Ortstafeln name the Ortschaften of a Gemeinde (Overture localadmin), points in the data."""

    def test_villages_of_a_gemeinde(self):
        """Lauterbrunnen's Gemeinde: its villages one town (no sign between them grown together); a Ticino circolo's
        point no place; a homonym's canton its small line."""
        xy = [[np.array([(0, 0), (3000, 0), (3000, 1000), (0, 1000)], np.float64)]]
        ll = [[r / M for r in rings] for rings in xy]
        com = ts.Commune("Lauterbrunnen", xy, ll, None, "g-1", "", "CH")
        pts = VillageTest.Points([("p-1", "Lauterbrunnen", "", 500 / M, 500 / M),
                                  ("p-2", "Colombier (VD)", "", 1500 / M, 500 / M),
                                  ("p-3", "Circolo della Maggia", "", 2500 / M, 500 / M),
                                  ("p-4", "Reinach BL", "", 2800 / M, 500 / M)])
        pts.attrs["country"] = np.array(["CH"] * 4)
        v = ts.split_villages([com], lambda c: [pts])
        self.assertEqual([(c.name, c.sub) for c in v],
                         [("Lauterbrunnen", ""), ("Colombier", "VD"), ("Reinach", "BL")])
        self.assertEqual({c.group for c in v}, {"g-1"})
        self.assertEqual(ts._towns(v).tolist(), [0, 0, 0, -1])
        self.assertIsNone(ts.CANTON.match("Lyss"))
        self.assertIsNone(ts.CANTON.match("Les Bois"), "a word that is no canton")

    def test_main_and_minor_roads(self):
        """Blue on a main road, white on another; two faces; the exit face has no town name, the next place."""
        blue = np.asarray(ts.face_image("Wengen", ts.G_ENTRY, "", "CH", main=True))
        white = np.asarray(ts.face_image("Wengen", ts.G_ENTRY, "", "CH"))
        mid = blue.shape[0] // 2, 30
        self.assertEqual(tuple(blue[mid]), ts.CH_BLUE)
        self.assertEqual(tuple(white[mid]), ts.FACE_WHITE)
        self.assertNotEqual(ts.face_group("Wengen", ts.G_ENTRY, "", "CH", main=True),
                            ts.face_group("Wengen", ts.G_ENTRY, "", "CH"))
        self.assertFalse((np.asarray(ts.face_image("Wengen", ts.G_EXIT, "", "CH", main=True)) == ts.FACE_WHITE)
                         .all(axis=2)[round(0.5 * blue.shape[0]):-60, 60:-60].any(), "no next place: its field blank")
        with_next = np.asarray(ts.face_image("Wengen", ts.G_EXIT, "", "CH", "Lauterbrunnen", 4, main=True))
        self.assertTrue((with_next == ts.FACE_WHITE).all(axis=2)[round(0.5 * blue.shape[0]):-60, 60:-60].any())


class ItalyTest(unittest.TestCase):
    """Italy's signs name the frazione, its comune's frazioni one town; a bilingual name on two lines, a Friulian
    one under the Italian."""

    def test_frazioni_and_their_names(self):
        xy = [[np.array([(0, 0), (3000, 0), (3000, 1000), (0, 1000)], np.float64)]]
        ll = [[r / M for r in rings] for rings in xy]
        com = ts.Commune("Udine", xy, ll, None, "u-1", "", "IT", county="Udine")
        pts = VillageTest.Points([("p-1", "Udine", "", 500 / M, 500 / M),
                                  ("p-2", "San Giovanni al Natisone / San Zuan dal Nadison", "", 1500 / M, 500 / M)])
        pts.attrs["country"] = np.array(["IT"] * 2)
        pts.attrs["name_fur"] = np.array(["Udin", "San Zuan dal Nadison"])
        pts.attrs["localities"] = np.array(["region:r=Friuli-Venezia Giulia|county:c=Udine|locality:u-1=Udine",
                                            "region:r=Friuli-Venezia Giulia|county:c=Udine|locality:u-1=Udine|"
                                            "locality:p-2=San Giovanni al Natisone / San Zuan dal Nadison"])
        v = ts.split_villages([com], lambda c: [pts])
        self.assertEqual([(c.name, c.local) for c in v],
                         [("Udine", "Udin"), ("San Giovanni al Natisone / San Zuan dal Nadison", "")],
                         "a name with both already: no second line")
        self.assertEqual(ts._towns(v).tolist(), [0, 0, -1])

    def test_frazioni_grown_into_the_city(self):
        """Barcola grown into Trieste: its signs say TRIESTE; Prosecco, apart, its own."""
        xy = [[np.array([(0, 0), (4000, 0), (4000, 1000), (0, 1000)], np.float64)]]
        ll = [[r / M for r in rings] for rings in xy]
        com = ts.Commune("Trieste", xy, ll, None, "t-1", "", "IT")
        # (Gretta's point in the fields past the city's last houses, its part of the comune's houses the city's)
        pts = VillageTest.Points([("p-1", "Trieste", "", 500 / M, 500 / M), ("p-2", "Barcola", "", 1500 / M, 500 / M),
                                  ("p-3", "Prosecco", "", 3300 / M, 500 / M), ("p-4", "Gretta", "", 2300 / M, 500 / M)])
        pts.attrs["country"] = np.array(["IT"] * 4)
        pts.attrs["localities"] = np.array([f"locality:t-1=Trieste|locality:{p}=x"
                                            for p in ("p-1", "p-2", "p-3", "p-4")])
        v = ts.split_villages([com], lambda c: [pts])

        def urban(lat, lon):
            x = np.asarray(lon) * M
            return ((x > 100) & (x < 2000)) | ((x > 3000) & (x < 3600))
        v = ts.with_agglomerations(v, lambda c: ts.commune_agglomeration(c, urban))
        self.assertEqual([c.name for c in v], ["Trieste", "Trieste", "Prosecco", "Trieste"])
        # a Danish kommune is named after its seat with a word more: Roskilde Kommune, Roskilde
        dk = ts.split_villages([com._replace(name="Roskilde Kommune", country="DK")], lambda c: [self._points(
            [("p-1", "Roskilde"), ("p-2", "Bakkehuse"), ("p-3", "Svogerslev")], "DK")])
        dk = ts.with_agglomerations(dk, lambda c: ts.commune_agglomeration(c, urban))
        self.assertEqual([c.name for c in dk], ["Roskilde", "Roskilde", "Svogerslev"])

    @staticmethod
    def _points(names, cc):
        """Village points at x 500, 1500, 3300 m, of country cc."""
        pts = VillageTest.Points([(fid, n, "", x / M, 500 / M) for (fid, n), x in zip(names, (500, 1500, 3300))])
        pts.attrs["country"] = np.array([cc] * len(names))
        return pts

    def test_face(self):
        def ink_rows(img):              # (between the border's sides)
            return np.flatnonzero((np.asarray(img)[:, 100:-100].sum(axis=2) == 0).any(axis=1))
        one, two = ink_rows(ts.face_image("Bolzano", ts.G_ENTRY, "", "IT")), \
            ink_rows(ts.face_image("Bolzano - Bozen", ts.G_ENTRY, "", "IT"))
        self.assertEqual(ts.BILINGUAL.split("Casteddu/Cagliari"), ["Casteddu", "Cagliari"])
        # (the border's rows aside: the letters' rows, two lines' a longer span)
        inner = lambda r: r[(r > 40) & (r < ts.PLATE_H * ts.FACE_PX_PER_M - 40)]
        self.assertGreater(np.ptp(inner(two)), np.ptp(inner(one)))
        red = (np.asarray(ts.face_image("Bolzano", ts.G_EXIT, "", "IT")) == ts.IT_RED).all(axis=2)
        self.assertTrue(red.any(), "the exit's red band")


class SpainTest(unittest.TestCase):
    def test_comarca_and_municipios(self):
        """A comarca around its municipios: their núcleos theirs, the comarca no town; a municipio's núcleos one
        town."""
        def com(name, fid, x0, x1):
            xy = [[np.array([(x0, 0), (x1, 0), (x1, 1000), (x0, 1000)], np.float64)]]
            return ts.Commune(name, xy, [[r / M for r in rings] for rings in xy], None, fid, "", "ES")
        comarca, reus, other = com("Baix Camp", "c-0", 0, 4000), com("Reus", "m-1", 0, 2000), \
            com("Riudoms", "m-2", 2000, 4000)
        pts = VillageTest.Points([("p-1", "Reus", "", 500 / M, 500 / M), ("p-2", "Mas Iglesias", "", 1500 / M, 500 / M),
                                  ("p-3", "Riudoms", "", 3000 / M, 500 / M)])
        pts.attrs["country"] = np.array(["ES"] * 3)
        v = ts.split_villages([comarca, reus, other], lambda c: [pts])
        self.assertEqual(sorted((c.name, c.group) for c in v),
                         [("Mas Iglesias", "m-1"), ("Reus", "m-1"), ("Riudoms", "m-2")])


class LatinTest(unittest.TestCase):
    def test_latin_name(self):
        """A Cyrillic or Greek name's Latin form, as the signs write it: Serbian's own, the others' alternate."""
        names = {"name_srlatn": "Beograd", "name_alt": "Belgrade"}
        self.assertEqual(ts.latin_name(names, "Београд", "RS"), "Beograd")
        self.assertEqual(ts.latin_name({"name_srlatn": "Sofija", "name_alt": "Sofia"}, "София", "BG"), "Sofia")
        self.assertEqual(ts.latin_name({"name_alt": "Сердика"}, "София", "BG"), "", "an alternate not Latin")
        self.assertEqual(ts.latin_name({"name_alt": "", "name_en": "Kalabaka"}, "Καλαμπάκα", "GR"), "Kalabaka",
                         "no alternate: the English name, a small place's transliteration")
        self.assertEqual(ts.latin_name({"name_alt": "Mostar"}, "Mostar", "RS"), "", "a Latin name already")
        self.assertEqual(ts.latin_name(names, "Београд", "FR"), "")
        self.assertTrue(ts.is_seat("Vilnius", "Vilniaus miesto savivaldybė"))
        self.assertTrue(ts.is_seat("Roskilde", "Roskilde Kommune"))
        self.assertTrue(ts.is_seat("Rapla", "Rapla vald"))
        self.assertEqual(ts.EE_KIND.sub("", "Mahlamäe küla"), "Mahlamäe")
        self.assertFalse(ts.is_seat("Vilkpėdė", "Vilniaus miesto savivaldybė"))

    def test_marks(self):
        """The accents of the east's Latin names drawn: breve, comma, macron, ogonek, dot, ring, double acute."""
        self.assertEqual(ts.mark_text("Vălenii")[1], {1: ["breve"]})
        self.assertEqual(ts.mark_text("Mureș")[1], {4: ["comma"]})
        self.assertEqual(ts.mark_text("Győr")[1], {2: ["double_acute"]})
        self.assertEqual(ts.mark_text("Београд"), ("Београд", {}), "Cyrillic as it is: the system's typeface")


class NorthAndBalkansTest(unittest.TestCase):
    def test_croatias_districts(self):
        """Zagreb's districts are areas in Zagreb's: no town of their own; Sesvete, beside it, is."""
        rows = [("z", "locality", "Zagreb", square(0, 0, 0.04, 0.04), {}),
                ("m", "locality", "Maksimir", square(0.01, 0.01, 0.02, 0.02), {}),
                ("s", "locality", "Sesvete", square(0.04, 0, 0.06, 0.02), {})]
        feats = Divisions(rows)
        feats.attrs["country"] = np.array(["HR"] * len(rows))
        self.assertEqual([c.name for c in ts.communes_of([feats], FRAME)], ["Sesvete", "Zagreb"])

    def test_norways_kommune(self):
        """A Norwegian kommune's places grown into its seat say its name: Gyldenpris, Bergen's."""
        xy = [[np.array([(0, 0), (4000, 0), (4000, 1000), (0, 1000)], np.float64)]]
        com = ts.Commune("Bergen", xy, [[r / M for r in rings] for rings in xy], None, "k-1", "", "NO")
        v = ts.split_villages([com], lambda c: [ItalyTest._points(
            [("p-1", "Bergen"), ("p-2", "Gyldenpris"), ("p-3", "Indre Arna")], "NO")])

        def urban(lat, lon):
            x = np.asarray(lon) * M
            return ((x > 100) & (x < 2000)) | ((x > 3000) & (x < 3600))
        v = ts.with_agglomerations(v, lambda c: ts.commune_agglomeration(c, urban))
        self.assertEqual([c.name for c in v], ["Bergen", "Bergen", "Indre Arna"])

    def test_faces(self):
        """Norway's name sign marks no limit: its back the same, no red bar; Iceland's crossed, its ð drawn by the
        system's typeface."""
        def red(cc, name):
            return (np.asarray(ts.face_image(name, ts.G_EXIT, "", cc)) == ts.FACE_RED).all(axis=2).any()
        self.assertFalse(red("NO", "Bergen"))
        self.assertTrue(red("IS", "Kópavogur"))
        self.assertTrue(red("HR", "Split"))
        self.assertEqual(ts.mark_text("Ólafsfjörður"), ("Ólafsfjörður", {}))

    def test_seats_and_neighbours_signs(self):
        """A bashkia's, an opština's, a Kosovar komuna's seat (one of its names); Andorra's, Liechtenstein's and San
        Marino's signs their neighbours'; Moldova's name on its white band."""
        self.assertTrue(ts.is_seat("Shkodër", "Bashkia Shkodër"))
        self.assertTrue(ts.is_seat("Bar", "Opština Bar"))
        self.assertTrue(ts.is_seat("Prizren", "Komuna e Prizrenit / Opština Prizren / Prizren Belediyesi"))
        self.assertFalse(ts.is_seat("Kurilla", "Komuna e Prizrenit / Opština Prizren / Prizren Belediyesi"))
        self.assertEqual(ts.NAME_EXTRA["AL"].sub("", "Kullajxhi (Bardhaj)"), "Kullajxhi")
        self.assertEqual(ts.NAME_EXTRA["ME"].sub("", "Pečurice-dio"), "Pečurice")

    def test_british_isles_malta_cyprus(self):
        """Welsh first in Wales (a county's own villages: its name the VILLAGE_LANGS key); one-sided signs' backs
        blank; Ireland green on a national road; Greek capitals without their accents; Malta's article left out."""
        xy = [[np.array([(0, 0), (2000, 0), (2000, 1000), (0, 1000)], np.float64)]]
        com = ts.Commune("Carmarthenshire", xy, [[r / M for r in rings] for rings in xy], None, "c-1", "", "GB")
        pts = VillageTest.Points([("p-1", "Llandovery", "", 500 / M, 500 / M)])
        pts.attrs["country"] = np.array(["GB"])
        pts.attrs["name_cy"] = np.array(["Llanymddyfri"])
        self.assertEqual([(c.name, c.local) for c in ts.split_villages([com], lambda c: [pts])],
                         [("Llandovery", "Llanymddyfri")])
        for cc in ("GB", "IE", "MT", "CY"):
            back = np.asarray(ts.face_image("Ennis", ts.G_EXIT, "", cc))
            inner = back[round(0.2 * back.shape[0]):round(0.8 * back.shape[0]), 60:-60]
            self.assertTrue((inner == inner[0, 0]).all(), cc + ": a blank back")
        green = np.asarray(ts.face_image("Killarney", ts.G_ENTRY, "Cill Airne", "IE", main=True))
        self.assertEqual(tuple(green[green.shape[0] // 2, 40]), ts.IE_GREEN)
        self.assertEqual(ts.sign_upper("Κάλο Χωριό"), "ΚΑΛΟ ΧΩΡΙΟ")
        self.assertEqual(ts.sign_upper("Plzeň"), "PLZEŇ")
        self.assertEqual(ts.NAME_EXTRA["MT"].sub("", "Il-Mosta"), "Mosta")
        self.assertEqual(ts.NAME_EXTRA["MT"].sub("", "Ħal Lija"), "Ħal Lija")
        self.assertEqual(ts.NAME_EXTRA["CY"].sub("", "Κοινότητα Χλώρακα"), "Χλώρακα")
        self.assertTrue(ts.MUNICIPALITY_POINTS.search("Paphos Industrial area"))

    def test_north_america(self):
        """A US sign says CITY LIMIT under the name; Ontario's blue, the other provinces' green (Commune.sub: the
        province's style); a Canadian county no city, its single-tier city one; a municipio's seat."""
        us = np.asarray(ts.face_image("Columbus", ts.G_ENTRY, "", "US"))
        low = us[round(0.68 * us.shape[0]):round(0.85 * us.shape[0]), 60:-60]
        self.assertTrue((low == ts.FACE_WHITE).all(axis=2).any(), "CITY LIMIT under the name")
        on = np.asarray(ts.face_image("Forthton", ts.G_ENTRY, "", "CA", sub="blue"))
        qc = np.asarray(ts.face_image("Amos", ts.G_ENTRY, "", "CA"))
        self.assertEqual(tuple(on[on.shape[0] // 2, on.shape[1] // 28]), ts.CH_BLUE)
        self.assertEqual(tuple(qc[qc.shape[0] // 2, qc.shape[1] // 28]), ts.NA_GREEN)
        self.assertEqual(ts.village_sub("Forthton", ts.Commune("X", [], [], None, "c", "", "CA", sub="blue")), "blue")
        self.assertFalse(ts.kreisfreie("Frontenac County"))
        self.assertTrue(ts.is_seat("Querétaro", "Municipio de Querétaro"))

    def test_arabic_shaped(self):
        """An Arabic name's letters joined (their presentation forms), lam and alef one ligature, read right to left
        (drawn reversed)."""
        self.assertEqual(ts.arabic_shape("بوثلجة"), "ﺔﺠﻠﺛﻮﺑ")
        self.assertEqual(ts.arabic_shape("لا"), "ﻻ")
        self.assertEqual(ts.mark_text("مكناس")[0], ts.arabic_shape("مكناس"))

    def test_scripts(self):
        """A name in several scripts a line each (Arabic, Ge'ez, Tifinagh, then Latin); Hebrew reversed; Georgian not
        capitalised; the areas and points that are no place."""
        self.assertEqual(ts.split_scripts("Meknès ⴰⵎⴽⵏⴰⵙ مكناس"), ["مكناس", "ⴰⵎⴽⵏⴰⵙ", "Meknès"])
        self.assertEqual(ts.split_scripts("ኣስመራ Asmara أسمرة"), ["أسمرة", "ኣስመራ", "Asmara"])
        self.assertEqual(ts.mark_text("בית שמש")[0], "שמש תיב")
        self.assertEqual(ts.sign_upper("გორი"), "გორი")
        self.assertTrue(ts.MUNICIPALITY_POINTS.search("Barut ward"))
        self.assertTrue(ts.MUNICIPALITY_POINTS.search("Asokwa Municipal District"))
        self.assertTrue(ts.AREA_NOT_PLACE.match("دهستان فجر"))
        self.assertEqual(ts.NAME_EXTRA["IR"].sub("", "شهر کاشان"), "کاشان")
        self.assertTrue(ts.NEIGHBOURHOOD_POINTS.match("حي السعادة"))
        self.assertTrue(ts.is_seat("Jinja", "Jinja City"))
        self.assertTrue(ts.is_seat("Ségou", "Cercle de Ségou"))
        self.assertTrue(ts.is_seat("زحلة", "قضاء زحلة"))
        self.assertTrue(ts.is_seat("Maseru", "Maseru (Maseru District)"))

    def test_central_america(self):
        """A neighbourhood, an estate or a district point is no place; a district's seat; the guide sign countries'
        green, Cuba's navy crossed on the way out."""
        for n in ("Colonia Bendeck", "Zona 2", "Condominio Residencial Portal de la Escalón", "Finca La Joya", "IV"):
            self.assertTrue(ts.NEIGHBOURHOOD_POINTS.match(n), n)
        for n in ("Comayagua", "Villa Nueva", "Santa Ana", "Granada", "Isabel Villas"):
            self.assertIsNone(ts.NEIGHBOURHOOD_POINTS.match(n), n)
        self.assertTrue(ts.is_seat("Panamá", "Distrito de Panamá"))
        self.assertTrue(ts.is_seat("Santiago de los Caballeros", "Santiago"))
        self.assertFalse(ts.is_seat("Santiago Rodríguez", "Santiago"))
        self.assertTrue(ts.is_seat("Zipaquirá", "Zipaquira"))
        self.assertTrue(ts.MUNICIPALITY_POINTS.search("Dayrells Commercial Park"))
        self.assertIs(ts.PLAINS["GT"], ts.PLAINS["MX"])
        cu = np.asarray(ts.face_image("Jicotea", ts.G_EXIT, "", "CU"))
        self.assertTrue((cu == ts.FACE_RED).all(axis=2).any(), "Cuba's exit crossed")

    def test_ukraine_and_belarus(self):
        """Ukraine's Latin line its English name (the official transliteration), not the alternate (the Russian's);
        a hromada's and a selsoviet's points no place; a Belarusian raion no city, its city one."""
        self.assertEqual(ts.latin_name({"name_en": "Odesa", "name_alt": "Odessa"}, "Одеса", "UA"), "Odesa")
        self.assertTrue(ts.MUNICIPALITY_POINTS.search("Вінницька міська громада"))
        self.assertTrue(ts.MUNICIPALITY_POINTS.search("Гаранскі сельскі Савет"))
        self.assertIsNone(ts.MUNICIPALITY_POINTS.search("Вінниця"))
        self.assertFalse(ts.kreisfreie("Баранавіцкі раён"))
        self.assertTrue(ts.kreisfreie("Баранавічы"))
        with_latin = np.asarray(ts.face_image("Одеса", ts.G_ENTRY, "Odesa", "UA"))
        alone = np.asarray(ts.face_image("Одеса", ts.G_ENTRY, "", "UA"))
        self.assertFalse((with_latin == alone).all(), "the Latin line drawn")
        for cc, like in (("AD", "ES"), ("LI", "CH"), ("SM", "IT")):
            self.assertTrue((np.asarray(ts.face_image("X", ts.G_ENTRY, "", cc))
                             == np.asarray(ts.face_image("X", ts.G_ENTRY, "", like))).all(), cc)
        md = np.asarray(ts.face_image("Orhei", ts.G_ENTRY, "", "MD"))
        h = md.shape[0]
        self.assertEqual(tuple(md[round(0.25 * h), 40]), ts.YELLOW)
        self.assertEqual(tuple(md[round(0.75 * h), 40]), ts.FACE_WHITE)


class PictogramTest(unittest.TestCase):
    def test_squares(self):
        """Sweden's signs name no place: squares of a fixed grid over the cells, one town, their built-up land within
        the country's counties; a square with none left out; the same squares whichever box asks."""
        county = [[np.array([(0.0, 0.0), (0.06, 0.0), (0.06, 0.06), (0.0, 0.06), (0.0, 0.0)])]]
        sq = ts.squares({"SE": [(ts._box(county), county)]}, (0.01, 0.01, 0.03, 0.03), FRAME)
        self.assertEqual({c.group for c in sq}, {"SE:sq"})
        self.assertTrue(all(c.name == "" and c.within is not None for c in sq))
        again = ts.squares({"SE": [(ts._box(county), county)]}, (0.015, 0.015, 0.025, 0.025), FRAME)
        self.assertTrue({c.fid for c in again} <= {c.fid for c in sq})

        def urban(lat, lon):            # (a town at lon 0.01 - 0.02, another past the counties at lon -0.03)
            lon = np.asarray(lon)
            return ((lon > 0.01) & (lon < 0.02)) | ((lon > -0.03) & (lon < -0.02))
        kept = ts.with_agglomerations(sq, lambda c: ts.commune_agglomeration(c, urban))
        self.assertTrue(kept)
        for c in kept:                  # (none of the built-up land outside the country)
            lon = c.agglo.lon0 + (np.nonzero(c.agglo.mask.any(axis=0))[0] + 0.5) * c.agglo.dlon
            self.assertGreater(lon.min(), -0.001)


class DutchTest(unittest.TestCase):
    def test_woonplaatsen(self):
        """A Dutch woonplaats: its gemeente under it when that has another name, Friesland's Frisian name only."""
        # (each woonplaats in its gemeente, the gemeente a little larger)
        rows, regions = [], []
        for k, (place, gem, fy, region) in enumerate([("Bunde", "Meerssen", "Bûnde", "NL-LI"),
                                                      ("Sneek", "Súdwest-Fryslân", "Snits", "NL-FR"),
                                                      ("Zandvoort", "Zandvoort", "Sânfoart", "NL-NH")]):
            x = 5.7 + 0.1 * k
            rows.append((f"w-{k}", "locality", place, square(x, 50.0, x + 0.02, 50.02), {"fy": fy}))
            rows.append((f"g-{k}", "county", gem, square(x - 0.01, 49.99, x + 0.03, 50.03), {}))
            regions += [region, region]
        div = Divisions(rows)
        div.attrs["country"] = np.array(["NL"] * len(rows))
        div.attrs["region"] = np.array(regions)
        got = {c.name: (c.local, c.sub) for c in ts.communes_of([div], FRAME)}
        self.assertEqual(got, {"Bunde": ("", "gem. Meerssen"), "Sneek": ("Snits", "gem. Súdwest-Fryslân"),
                               "Zandvoort": ("", "")})


class GermanTownTest(unittest.TestCase):
    """Which town a German village is part of (its point's hierarchy, town_of): its Ortstafel's name and line."""

    def test_town_of(self):
        self.assertEqual(ts.town_of("region:r=Hessen|county:c=Rheingau-Taunus-Kreis|locality:a=Rüdesheim am Rhein|"
                                    "locality:b=Assmannshausen"), ("a", "Rüdesheim am Rhein"))
        self.assertEqual(ts.town_of("region:r=Niedersachsen|county:c=Landkreis Nienburg/Weser|"
                                    "locality:s=Samtgemeinde Mittelweser|locality:h=Husum"), ("h", "Husum"),
                         "an association of Gemeinden is no town")
        cities = {"Ulm"}
        self.assertEqual(ts.town_of("region:r=Baden-Württemberg|county:u=Ulm|locality:g=Grimmelfingen", cities),
                         ("u", "=Ulm"), "a kreisfreie Stadt's Stadtteil: the city")
        self.assertEqual(ts.town_of("region:b=Berlin|locality:p=Pankow|locality:q=Prenzlauer Berg", cities),
                         ("b", "=Berlin"))
        self.assertEqual(ts.town_of("region:s=Sachsen|county:z=Zwickau|locality:l=Langenweißbach|locality:g=Grünau",
                                    cities), ("l", "Langenweißbach"), "Landkreis Zwickau, named without Kreis: no city")
        self.assertFalse(ts.kreisfreie("Kreis Steinburg"))
        # by its size: Ulm (119 km2) a city, Börde (2366 km2, named without Kreis) a Landkreis
        ulm = [[np.array([(9.9, 48.35), (10.05, 48.35), (10.05, 48.45), (9.9, 48.45)])]]
        boerde = [[np.array([(11.0, 52.0), (11.7, 52.0), (11.7, 52.3), (11.0, 52.3)])]]
        self.assertTrue(ts.kreisfreie("Ulm", ulm))
        self.assertFalse(ts.kreisfreie("Börde", boerde))

    def test_the_small_line(self):
        de = ts.Commune("Rüdesheim am Rhein", [], [], None, "x", "", "DE", county="Rheingau-Taunus-Kreis")
        self.assertEqual(ts.village_sub("Assmannshausen", de, "Rüdesheim am Rhein"), "Rüdesheim am Rhein")
        self.assertEqual(ts.village_sub("Rüdesheim am Rhein", de, "Rüdesheim am Rhein"), "Rheingau-Taunus-Kreis")
        self.assertEqual(ts.village_sub("Grünau", de._replace(county="Zwickau"), ""), "Landkreis Zwickau")
        self.assertEqual(ts.village_sub("Ulm", de._replace(county="Ulm"), ""), "", "a kreisfreie Stadt: no Kreis")

    def test_austria(self):
        """Austria's Statutarstädte are its counties named without Bezirk; an Ortstafel's small line is the Gemeinde
        only, no Bezirk; a Carinthian bilingual name on two lines."""
        self.assertFalse(ts.kreisfreie("Bezirk Bregenz"))
        self.assertTrue(ts.kreisfreie("Linz"))
        at = ts.Commune("Untertauern", [], [], None, "x", "", "AT", county="Bezirk St. Johann im Pongau")
        self.assertEqual(ts.village_sub("Obertauern", at, "Untertauern"), "Untertauern")
        self.assertEqual(ts.village_sub("Untertauern", at, "Untertauern"), "")
        self.assertEqual(ts.village_sub("Urfahr", at, "=Linz"), "Linz")

        def ink_rows(img):
            return np.flatnonzero((np.asarray(img)[60:-60, 100:-100].sum(axis=2) == 0).any(axis=1))
        self.assertGreater(np.ptp(ink_rows(ts.face_image("Dellach/Dole", ts.G_ENTRY, "", "AT"))),
                           np.ptp(ink_rows(ts.face_image("Dellach", ts.G_ENTRY, "", "AT"))))


class NextPlaceTest(unittest.TestCase):
    def test_the_next_village_ahead(self):
        """Leaving eastward: the village straight ahead, not the one behind, nor one too far off the heading or too
        near, nor the town itself; a village as near to the side comes after one straight ahead."""
        pts = [("Behind", 5.98, 49.0), ("Ahead", 6.02, 49.0), ("Side", 6.0, 49.016), ("Me", 6.01, 49.0),
               ("Close", 6.001, 49.0), ("Far", 6.2, 49.0), ("Slightly", 6.019, 49.004)]
        self.assertEqual(ts.next_place(49.0, 6.0, 0.0, pts, "Me")[0], "Ahead")
        self.assertIsNone(ts.next_place(49.0, 6.0, 180.0, [p for p in pts if p[0] != "Behind"], "Me"))
        self.assertEqual(ts.next_place(49.0, 6.0, 90.0, pts, "Me")[0], "Side")

    def test_an_exit_face_with_it(self):
        """Its own face (another group) for each next place."""
        a = ts.face_group("Hesperange", ts.G_EXIT, "Hesper", "LU", "Howald")
        self.assertNotEqual(a, ts.face_group("Hesperange", ts.G_EXIT, "Hesper", "LU"))
        self.assertTrue(a.startswith("townout_lu_"))
        img = ts.face_image("Hesperange", ts.G_EXIT, "Hesper", "LU", "Howald")
        self.assertEqual(img.size, (round(ts.PLATES["LU"] * ts.FACE_PX_PER_M), round(ts.PLATE_H * ts.FACE_PX_PER_M)))


class RegionalNameTest(unittest.TestCase):
    """The regional name under the French one (the user, 2026-10-28: "en Catalogne française ... le nom du
    village/ville en fr puis en catalan"): the language of the commune's département, not any name the data has."""

    def test_the_departements_language(self):
        """Perpignan in the Pyrénées-Orientales gets its Catalan name, not its Occitan one; Toulouse, next door in the
        Haute-Garonne, its Occitan name, though it has a Catalan one too."""
        cat = {"ca": "Perpinyà", "oc": "Perpinhan", "eu": "Perpinyà"}
        occ = {"ca": "Tolosa", "oc": "Tolosa", "eu": "Tolosa Okzitania"}
        feats = Divisions([("c-po", "county", "Pyrénées-Orientales", square(0.0, 0.0, 0.1, 0.1), {}),
                           ("c-hg", "county", "Haute-Garonne", square(0.1, 0.0, 0.2, 0.1), {}),
                           ("l-p", "locality", "Perpignan", square(0.02, 0.02, 0.04, 0.04), cat),
                           ("l-t", "locality", "Toulouse", square(0.12, 0.02, 0.14, 0.04), occ)])
        got = {c.name: c.local for c in ts.communes_of([feats], FRAME)}
        self.assertEqual(got, {"Perpignan": "Perpinyà", "Toulouse": "Tolosa"})

    def test_no_regional_language_no_second_name(self):
        """Rennes is in Brittany (Breton), Strasbourg in Alsace; Lyon's Rhône has no regional language on its signs,
        whatever names the data gives its towns; a name the sign would write as the French one is no second name."""
        def local(county, name, names, localadmin=""):
            return ts.local_name(name, county, localadmin, names)
        self.assertEqual(local("Ille-et-Vilaine", "Rennes", {"br": "Roazhon", "oc": "Roazhon"}), "Roazhon")
        self.assertEqual(local("Bas-Rhin", "Strasbourg", {"gsw": "Strossbüri", "oc": "Estrasborg"}), "Strossbüri")
        self.assertEqual(local("Rhône", "Lyon", {"oc": "Lion"}), "")
        self.assertEqual(local("Gard", "Nîmes", {"oc": "Nimes"}), "", "NIMES both ways")
        self.assertEqual(local("Pyrénées-Orientales", "Llupia", {"ca": "Llupià"}), "Llupià", "its accent counts")
        self.assertEqual(local("Nord", "Hazebrouck", {"vls": "Haezebroek / Oazebroeke"}, "Dunkerque"), "Haezebroek")
        self.assertEqual(local("Nord", "Lille", {"vls": "Rysel"}, "Lille"), "", "French Flanders is the Westhoek")
        self.assertEqual(local("Corrèze", "Brive-la-Gaillarde", {"oc": "Briva (La Galharda)"}), "Briva")

    def test_the_basque_country_and_the_bearn(self):
        """The Pyrénées-Atlantiques: the arrondissement of Bayonne is Basque (Bayonne: Baiona); the Béarn's towns
        get their Occitan names though the data gives them Basque ones too (Pau: not Paue); the Soule's villages have
        only Basque names (Ordiarp: Urdiñarbe)."""
        pa = "Pyrénées-Atlantiques"
        self.assertEqual(ts.local_name("Saint-Jean-Pied-de-Port", pa, "Bayonne",
                                       {"eu": "Donibane-Garazi", "oc": "Sent Joan deu Pè deu Pòrt"}), "Donibane-Garazi")
        self.assertEqual(ts.local_name("Orthez", pa, "Pau", {"eu": "Ortheze", "oc": "Ortès"}), "Ortès")
        self.assertEqual(ts.local_name("Pau", pa, "Pau", {"eu": "Paue", "oc": "Pau"}), "", "Pau in Occitan too")
        self.assertEqual(ts.local_name("Ordiarp", pa, "Oloron-Sainte-Marie", {"eu": "Urdiñarbe"}), "Urdiñarbe")

    def test_inner_point_of_a_crescent(self):
        """A U-shaped commune: its vertices' mean lies in the U's gap; the point taken is inside it."""
        u = np.array([(0, 0), (3, 0), (3, 3), (2, 3), (2, 1), (1, 1), (1, 3), (0, 3), (0, 0)], np.float64)
        p = ts.inner_point([[u]])
        self.assertTrue(ts.inside([[u]], p[None, :])[0], p)
        self.assertEqual(ts.inner_point([[u]]).tolist(), p.tolist())

    def test_the_sign_carries_both_names(self):
        """A sign's rows name the commune and its regional name; the face is another with it."""
        village = commune("PRADES", 0, -1500, 2000, 1500, east_of(200.0))._replace(local="Prada")
        rows = signs([road("r")], [village])
        self.assertEqual({(r[8], r[9]) for r in rows}, {("PRADES", "Prada")})
        self.assertNotEqual(ts.face_group("Prades", ts.G_ENTRY, "Prada"), ts.face_group("Prades", ts.G_ENTRY))
        self.assertEqual(ts.face_group("Prades", ts.G_ENTRY, ""), ts.face_group("Prades", ts.G_ENTRY))


class FaceTest(unittest.TestCase):
    def test_the_name_in_capitals_without_accents(self):
        self.assertEqual(ts.sign_text("Saint-Christol-lez-Alès"), "SAINT-CHRISTOL-LEZ-ALES")
        self.assertEqual(ts.sign_text("Générargues"), "GENERARGUES")

    def test_long_names_on_two_lines_at_a_hyphen(self):
        self.assertEqual(ts._lines("BAGARD"), ["BAGARD"])
        self.assertEqual(ts._lines("SAINT-CHRISTOL-LEZ-ALES"), ["SAINT-CHRISTOL-", "LEZ-ALES"])
        self.assertEqual(ts._lines("MASSILLARGUESATTUECH"), ["MASSILLARGUESATTUECH"])

    def test_a_face_per_name_and_kind(self):
        """The same name gives the same face whatever the tile; entry and exit faces differ; another name another."""
        a = ts.face_group("Bagard", ts.G_ENTRY)
        self.assertEqual(a, ts.face_group("Bagard", ts.G_ENTRY))
        self.assertTrue(a.startswith("townin_"))
        self.assertTrue(ts.face_group("Bagard", ts.G_EXIT).startswith("townout_"))
        self.assertNotEqual(a, ts.face_group("Tornac", ts.G_ENTRY))

    def test_face_image(self):
        """The plate's proportions; white inside, the red border at its edge; the exit sign's red bar crosses it."""
        img = ts.face_image("Bagard", ts.G_ENTRY)
        self.assertEqual(img.size, (round(ts.PLATE_W * ts.FACE_PX_PER_M), round(ts.PLATE_H * ts.FACE_PX_PER_M)))
        w, h = img.size
        self.assertEqual(img.getpixel((w // 2, round(h * (ts.MARGIN_FRAC + ts.BORDER_FRAC / 2)))), ts.FACE_RED)
        corner = (round(w * 0.2), round(h * 0.25))          # white inside, above the name
        self.assertEqual(img.getpixel(corner), ts.FACE_WHITE)
        dark = int((np.asarray(img).sum(axis=2) == 0).sum())
        self.assertGreater(dark, 0.03 * w * h, "the name is written")
        out = ts.face_image("Bagard", ts.G_EXIT)
        self.assertEqual(out.getpixel((w // 2, h // 2 - 1)), ts.FACE_RED, "the bar crosses the middle")

    def test_face_image_with_the_regional_name(self):
        """The regional name under the French one: a second block of ink below, lighter than the French name's, both
        inside the border."""
        def ink(img):
            return np.asarray(img).sum(axis=2) == 0
        one = np.flatnonzero(ink(ts.face_image("Perpignan", ts.G_ENTRY)).any(axis=1))
        a = ink(ts.face_image("Perpignan", ts.G_ENTRY, "Perpinyà"))
        two = np.flatnonzero(a.any(axis=1))
        h = round(ts.PLATE_H * ts.FACE_PX_PER_M)
        self.assertGreater(two.max(), one.max(), "a second line below")
        gaps = np.flatnonzero(np.diff(two) > 1)
        self.assertEqual(len(gaps), 1, "two blocks of ink: the French name, then the Catalan one")
        cut = two[gaps[0]] + 1
        self.assertGreater(a[:cut].sum(), a[cut:].sum(), "the French name the larger and bolder")
        border = round(h * (ts.MARGIN_FRAC + ts.BORDER_FRAC))
        self.assertTrue(border < two.min() and two.max() < h - border)

    def test_the_regional_names_accents_are_drawn(self):
        """Pillow's own typeface has no accented letter: the marks are drawn over their letters (Tuïr's diaeresis:
        ink above the i that 'Tuir' has not)."""
        self.assertEqual(ts.mark_text("Tuïr"), ("Tuir", {2: ["diaeresis"]}))
        self.assertEqual(ts.mark_text("Perpinyà"), ("Perpinya", {7: ["grave"]}))
        self.assertEqual(ts.mark_text("Çà"), ("Ca", {0: ["cedilla"], 1: ["grave"]}))
        plain = np.asarray(ts.face_image("Thuir", ts.G_ENTRY, "Tuir")).sum(axis=2) == 0
        marked = np.asarray(ts.face_image("Thuir", ts.G_ENTRY, "Tuïr")).sum(axis=2) == 0
        self.assertGreater(int(marked.sum()), int(plain.sum()), "the diaeresis drawn")

    def test_belgium_has_its_own_sign(self):
        """Belgium's F1a: white with a black border, the name over a village's black silhouette (black ink in the lower
        middle, where the French sign has none); the F3a's red bar; a name in two countries, two faces; Brussels' two
        names on two lines."""
        be = np.asarray(ts.face_image("Namur", ts.G_ENTRY, "", "BE"))
        fr = np.asarray(ts.face_image("Namur", ts.G_ENTRY))
        h, w = be.shape[:2]
        x = round(h * (ts.MARGIN_FRAC + ts.BE_BORDER_FRAC / 2))
        self.assertEqual(tuple(int(v) for v in be[h // 2, x]), (0, 0, 0), "a black border, not the French red")
        low = (slice(int(h * 0.72), int(h * 0.82)), slice(int(w * 0.45), int(w * 0.55)))
        self.assertGreater(int((be[low].sum(axis=2) == 0).sum()), int((fr[low].sum(axis=2) == 0).sum()) + 100,
                           "the silhouette under the name")
        out = np.asarray(ts.face_image("Namur", ts.G_EXIT, "", "BE"))
        self.assertTrue((np.abs(out[h // 2 - 3:h // 2 + 3, w // 2].astype(int) - ts.BE_RED).sum(axis=1) < 30).any(),
                        "the red bar")
        self.assertNotEqual(ts.face_group("Namur", ts.G_ENTRY, "", "BE"), ts.face_group("Namur", ts.G_ENTRY))

        def ink_rows(img):
            return np.flatnonzero((np.asarray(img)[:, :, :].sum(axis=2) == 0)[:, int(w * 0.3):int(w * 0.7)].any(axis=1))
        one = ink_rows(ts.face_image("Ixelles", ts.G_ENTRY, "", "BE"))
        two = ink_rows(ts.face_image("Ixelles - Elsene", ts.G_ENTRY, "", "BE"))
        self.assertGreater(len(np.flatnonzero(np.diff(two) > 1)), len(np.flatnonzero(np.diff(one) > 1)),
                           "one more line of ink: Elsene under Ixelles")

    def test_write_face_once(self):
        with tempfile.TemporaryDirectory() as d:
            e = ts.write_face(d, "/temp/beamngEarth/s", "Bagard", ts.G_ENTRY)
            g = ts.face_group("Bagard", ts.G_ENTRY)
            self.assertEqual(e, {"shape": f"/temp/beamngEarth/s/{g}.dae", "texture": f"/temp/beamngEarth/s/{g}.png",
                                 "material": "bne_" + g})
            text = Path(d, g + ".dae").read_text()
            self.assertIn(f'name="bne_{g}"', text)
            self.assertIn(f"<init_from>{g}.png</init_from>", text)
            t0 = os.stat(Path(d, g + ".png")).st_mtime_ns
            ts.write_face(d, "/temp/beamngEarth/s", "Bagard", ts.G_ENTRY)
            self.assertEqual(os.stat(Path(d, g + ".png")).st_mtime_ns, t0, "not written again")


class TileTest(unittest.TestCase):
    def test_the_tile_names_its_faces_and_the_revisit_cache_keeps_them(self):
        """write_tile's "townsigns" name the faces its props use (only those), and revisit.shapes_in finds both
        files of each, so a tile taken from the cache in another session has them."""
        with tempfile.TemporaryDirectory() as d:
            g = ts.face_group("Bagard", ts.G_ENTRY)
            faces = {g: ts.write_face(d, "/vfs", "Bagard", ts.G_ENTRY), "townin_unused": {"shape": "/vfs/x.dae"}}
            props = [[g, 1.0, 2.0, 3.0, 90.0, 1.0, ts.SIGN_H, 0.0]]
            path, _ = roads.write_tile(d, "/vfs", 1, 0, 0, [], props=props, towns=faces)
            text = Path(d, os.path.basename(path)).read_text()
            data = json.loads(text)
            self.assertEqual(list(data["townsigns"]), [g])
            self.assertEqual(data["props"]["groups"], [g])
            self.assertEqual(revisit.shapes_in(text, "/vfs"), sorted([g + ".dae", g + ".png"]))

    def test_maker_writes_each_face_once_and_rows_use_their_groups(self):
        """Towns.maker: the communes of the tile's cells (their agglomerations from the land cover, kept), rows of
        the faces' groups, each face written once."""
        village = commune("BAGARD", 0, -1500, 2000, 1500, east_of(200.0))

        class Towns(ts.Towns):
            def communes(self, frame, keys):
                return [village]
        with tempfile.TemporaryDirectory() as d:
            faces = {}
            rows = Towns(None, None).maker(FRAME, [], d, "/vfs", faces)([road("r")], BOX, False)
            self.assertEqual(sorted(r[0] for r in rows), sorted(faces))
            self.assertEqual({len(r) for r in rows}, {8})
            self.assertEqual(len(os.listdir(d)), 4)          # an entry and an exit face: .png and .dae each

    def test_communes_wait_for_the_land_cover(self):
        """A commune whose land cover is not all cached: no communes (the tile has no signs and is built again),
        and nothing kept; once it is, its agglomeration is kept."""
        class Feats:
            ids = np.array(["d-1"])
            attrs = {"country": np.array(["FR"]), "name": np.array(["Bagard"])}

            def __len__(self):
                return 1

            def parts(self, i):
                return [[np.array([(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)])]]

        class Cell:
            layers = {"divisions": Feats()}

        class Surface:
            complete = False

            def classes(self, lat, lon):
                return np.where(np.asarray(lon) > 0.005, roads.URBAN_CLASS, 0), self.complete
        s = Surface()

        class MD:
            arrives = False             # (whether the land cover a fetch asks for comes)

            def cell(self, k, layers, kind):
                return Cell()

            def has(self, k, layer):
                return s.complete

            def prefetch(self, k, layers):
                s.complete = s.complete or self.arrives
        with mock.patch.object(ts, "POINTS_WAIT_S", 0.3):
            towns = ts.Towns(MD(), s)
            self.assertIsNone(towns.communes(FRAME, ["k"]))
            self.assertEqual(towns._agglo, {})
            s.complete = True
            got = towns.communes(FRAME, ["k"])
            self.assertEqual([c.name for c in got], ["Bagard"])
            self.assertIsNotNone(got[0].agglo)
            self.assertIn("d-1", towns._agglo)
            # the land cover fetched and waited for: the signs at once (a first visit's tile is not asked for again)
            s.complete = False
            md = MD()
            md.arrives = True
            towns = ts.Towns(md, s)
            self.assertEqual([c.name for c in towns.communes(FRAME, ["k"])], ["Bagard"])


class ClearTest(unittest.TestCase):
    def test_street_furniture_cleared_around_a_sign(self):
        """A street tree, a bench or a bin within CLEAR_M of a sign is taken away (a row of street trees hid the signs
        in Boisset-et-Gaujac, 2026-10-28); one further, a lamp or a pole beside it, and every row of a tile with no
        sign stay."""
        import furniture
        sign = ["townin_x", 0.0, 0.0, 0.0, 0.0, 1.0, ts.SIGN_H, 0.0]
        props = [[furniture.G_STREET_TREE, 3.0, 0.0, 0, 0, 1, 8, 0], [furniture.G_BENCH, 0.0, 5.0, 0, 0, 1, 1, 0],
                 [furniture.G_BIN, -4.0, 0.0, 0, 0, 1, 1, 0], [furniture.G_STREET_TREE, 12.0, 0.0, 0, 0, 1, 8, 0],
                 [furniture.G_LAMP, 2.0, 0.0, 0, 0, 1, 9, 0], [furniture.G_POLE, 1.0, 1.0, 0, 0, 1, 9, 0]]
        kept = ts.clear_around(props, [sign])
        self.assertEqual([(r[0], r[1]) for r in kept], [(furniture.G_STREET_TREE, 12.0), (furniture.G_LAMP, 2.0),
                                                       (furniture.G_POLE, 1.0)])
        self.assertIs(ts.clear_around(props, []), props)


class InStepTest(unittest.TestCase):
    """The sign's measures are written in three places (townsigns.py, tools/generate_town_signs.py and the mod's
    townsigns.lua): they must agree, or the face floats off its body."""

    def test_the_tool_and_the_mod_agree(self):
        tree = ast.parse((ROOT / "tools/generate_town_signs.py").read_text(encoding="utf-8"))
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    names = [e.id for e in t.elts] if isinstance(t, ast.Tuple) else [getattr(t, "id", None)]
                    try:
                        vals = ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    vals = vals if isinstance(vals, tuple) else (vals,)
                    consts.update(zip(names, vals))
        self.assertEqual((consts["PLATE_W"], consts["PLATE_H"], consts["PLATE_Z0"], consts["SIGN_H"]),
                         (ts.PLATE_W, ts.PLATE_H, ts.PLATE_Z0, ts.SIGN_H))
        self.assertLess(ts.FACE_Y, consts["PLATE_Y_FRONT"], "the face in front of the body's plate")
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/townsigns.lua").read_text(encoding="utf-8")
        self.assertIn(f"M.HEIGHT_M = {ts.SIGN_H}", lua)
        self.assertIn("sign_town_body'", lua)
        self.assertTrue((ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/sign_town_body.dae")
                        .exists())
        # a country with its own plate: the same width in the tool, its body generated, its faces named for it
        self.assertEqual({k.upper(): v[0] for k, v in consts["PLATES"].items()}, ts.PLATES)
        for cc in ts.PLATES:
            self.assertTrue((ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth"
                             / f"sign_town_body_{cc.lower()}.dae").exists(), cc)
            self.assertTrue(ts.face_group("X", ts.G_ENTRY, "", cc).startswith(f"townin_{cc.lower()}_"))
            img = ts.face_image("X", ts.G_ENTRY, "", cc)
            self.assertEqual(img.size, (round(ts.PLATES[cc] * ts.FACE_PX_PER_M), round(ts.PLATE_H * ts.FACE_PX_PER_M)))


if __name__ == "__main__":
    unittest.main()
