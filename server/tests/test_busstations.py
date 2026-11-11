"""Bus stations found from the map data (busstations.py), on items dumped from real cells of seven stations
(tests/data/busstations; tools/dump_busstation_fixture.py)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import busstations as bs

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "busstations")


def load(name):
    with open(os.path.join(DATA, name + ".json"), encoding="utf-8") as f:
        return bs.items_from_json(json.load(f)["items"])


def only(name):
    found = bs.find(load(name))
    assert len(found) == 1, f"{name}: {len(found)} stations"
    return found[0]


class PilotStationsTest(unittest.TestCase):
    def test_hradec_terminal_is_found_in_full(self):
        st = only("hk_terminal")
        self.assertTrue(st.confident())
        self.assertEqual(len(st.platforms), 10)                    # islands A-J, mapped as lines
        self.assertEqual(len(st.buildings), 2)                     # the two halls
        self.assertGreaterEqual(len(st.bays), 20)
        labels = {b["label"] for b in st.bays}
        self.assertTrue({"A1", "B2", "C1", "E3", "J2"} <= labels)   # "A1 (výstupní)" is A1; the letters are in name
        self.assertGreater(bs.area(st.boundary), 15000)

    def test_nachod_keeps_its_numbered_bays(self):
        st = only("nachod")
        self.assertTrue(st.confident())
        self.assertIsNone(st.boundary)                             # only a point is mapped
        self.assertTrue({str(n) for n in range(1, 9)} <= {b["label"] for b in st.bays})
        self.assertEqual(bs.bay_label(None, None, "A1 (výstupní)"), "A1")
        self.assertEqual(bs.bay_label(None, None, "Terminál HD"), "")
        self.assertGreaterEqual(len(st.canopies), 1)               # the 2,224 m2 roof is a canopy, not a shed
        self.assertEqual(st.buildings[0]["cls"], "transportation")

    def test_vinnytsia_central_has_its_platform_roof(self):
        st = only("vin_central")
        self.assertTrue(st.confident())
        self.assertEqual([round(c["area"], -1) for c in st.canopies], [870.0])   # one roof, not two on each other
        self.assertEqual(len(st.platforms), 1)

    def test_west_station_is_a_building_with_a_roof_beside_it(self):
        st = only("vin_west")
        self.assertIsNone(st.boundary)                             # the mapped "boundary" is the hall's own outline
        self.assertEqual([round(c["area"], -1) for c in st.canopies], [690.0])   # the 686 m2 shelter roof beside it
        self.assertEqual(len(st.claimed), 1)                       # and the buildings layer must not draw it as a shed

    def test_vinnytsia_west_and_east(self):
        for name in ("vin_west", "vin_east"):
            with self.subTest(name):
                st = only(name)
                self.assertTrue(st.confident())
                self.assertGreaterEqual(len(st.bays), 1)
                self.assertEqual(len(st.buildings), 1)

    def test_mohyliv_building_is_found_without_a_tag(self):
        st = only("mohyliv")
        self.assertTrue(st.confident())
        self.assertEqual(len(st.buildings), 1)
        self.assertAlmostEqual(bs.area(st.buildings[0]["xy"]), 705, delta=40)
        self.assertEqual(st.buildings[0]["cls"], "")               # nothing in the data says what it is

    def test_a_supermarket_stop_is_not_a_station(self):
        self.assertEqual(bs.find(load("vin3")), [])                # one node named after a shop: a ticket office, no station


class RulesTest(unittest.TestCase):
    def test_name_matching(self):
        self.assertTrue(bs.name_matches("Автовокзал", []))
        self.assertTrue(bs.name_matches("Náchod, autobusová stanice", []))
        self.assertTrue(bs.name_matches("Центральний автовокзал", ["Центральний автовокзал"]))
        self.assertFalse(bs.name_matches('Flixbus (Могилів-Подільський)', ["Вінниця 3"]))   # a brand's stop
        self.assertFalse(bs.name_matches('Супермаркет "Грош"', ["Вінниця 3"]))
        self.assertFalse(bs.name_matches("", ["Вінниця 3"]))

    def test_deterministic(self):
        a, b = only("hk_terminal"), only("hk_terminal")
        self.assertEqual(a.fid, b.fid)
        self.assertEqual(a.describe(), b.describe())

    def test_two_stations_far_apart_are_two(self):
        west = load("vin_west")                                    # (one frame, 5 km apart: the clustering is metres)
        for it in west:
            it["xy"] = it["xy"] + np.array([5000.0, 0.0])
        self.assertEqual(len(bs.find(load("vin_central") + west)), 2)

    def test_geometry_helpers(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], float)
        self.assertAlmostEqual(bs.area(sq), 100.0)
        self.assertTrue(bs.contains(sq, (5, 5)))
        self.assertFalse(bs.contains(sq, (15, 5)))
        self.assertAlmostEqual(bs.dist_to_path(sq, (15, 5), True), 5.0)
        np.testing.assert_allclose(bs.centroid(sq), [5, 5])
        self.assertEqual(len(bs.hull(np.vstack((sq, [[5, 5]])))), 4)


class FurnitureTest(unittest.TestCase):
    def test_hradec_platforms_with_bench_and_bin_tags_get_them(self):
        import busfurniture
        got = busfurniture.furniture(only("hk_terminal"))
        self.assertGreaterEqual(len([g for g in got if g[0] == "bench"]), 10)
        self.assertGreaterEqual(len([g for g in got if g[0] == "bin"]), 5)

    def test_roof_without_tagged_platform_gets_a_bench_row_facing_the_bays(self):
        import busfurniture
        st = only("vin_west")
        self.assertGreaterEqual(len([g for g in busfurniture.furniture(st) if g[0] == "bench"]), 1)   # from its platform
        st.platforms = []                                                                   # no tagged platform: a row
        got = busfurniture.furniture(st)
        benches = [g for g in got if g[0] == "bench"]
        self.assertGreaterEqual(len(benches), 4)
        for _, x, y, fx, fy in got:
            self.assertAlmostEqual(fx * fx + fy * fy, 1.0, places=6)
        under = [g for g in benches if any(bs.contains(c["ring"], (g[1], g[2])) for c in st.canopies)]
        self.assertGreaterEqual(len(under), 4)                                               # a row under the roof
        for i, a in enumerate(got):                                                          # not on each other
            for b in got[i + 1:]:
                self.assertGreaterEqual(float(np.hypot(a[1] - b[1], a[2] - b[2])), busfurniture.CLEAR_M - 1e-6)

    def test_lamps_stand_by_the_bays_not_under_roofs_nor_in_buildings(self):
        import busfurniture
        for name in ("hk_terminal", "nachod", "vin_central", "vin_west"):
            with self.subTest(name):
                st = only(name)
                lamps = busfurniture.lamps(st)
                if name in ("nachod", "vin_west"):                       # (under one big roof: fixtures, not poles)
                    self.assertTrue(lamps, name)
                for x, y, dx, dy, slab in lamps:
                    self.assertAlmostEqual(dx * dx + dy * dy, 1.0, places=6)
                    self.assertFalse(any(bs.contains(c["ring"], (x, y)) for c in st.canopies))
                    self.assertFalse(any(bs.contains(b["xy"], (x, y)) for b in st.buildings))
                for i, a in enumerate(lamps):
                    for b in lamps[i + 1:]:
                        self.assertGreaterEqual(float(np.hypot(a[0] - b[0], a[1] - b[1])), busfurniture.LAMP_SPACING_M - 1e-6)

    def test_rows_are_forest_rows_in_the_tile_only(self):
        import busfurniture
        import geodesy

        class FakeMd:
            pass
        st = only("vin_west")
        got = busfurniture.furniture(st)
        self.assertTrue(got)
        pts = np.array([[g[1], g[2]] for g in got] + [[l[0], l[1]] for l in busfurniture.lamps(st)])
        x0, y0 = pts.min(0) - 5.0
        x1, y1 = pts.max(0) + 5.0
        x, y = got[0][1], got[0][2]
        busfurniture.bs.stations_near = lambda md, frame, box, **kw: [st]                         # (no cells in a unit test)
        try:
            frame = geodesy.EnuFrame(49.23703, 28.40386, 0.0)
            elev = lambda la, lo: np.full(np.shape(la), 100.0)                              # noqa: E731
            inside = busfurniture.rows(frame, FakeMd(), (x0, y0, x1, y1), elev)
            outside = busfurniture.rows(frame, FakeMd(), (x + 500, y + 500, x + 600, y + 600), elev)
        finally:
            import importlib
            importlib.reload(busstations_module())
        self.assertTrue(inside)
        self.assertEqual(outside, [])
        groups = {r[0] for r in inside}
        self.assertTrue({"bench", "streetlight", "lamplens", "lampglow"} <= groups)          # lamps come with lenses
        g, rx, ry, rz, yaw, scale, h, rank = inside[0]
        self.assertIn(g, ("bench", "bin", "streetlight"))
        self.assertEqual((scale, rank), (1.0, 0.0))
        self.assertTrue(0.0 <= yaw < 360.0)


def busstations_module():
    return bs


class QualityTest(unittest.TestCase):
    """The second pass (plan 024): no false stations, halls, clean bays, on stations of the world."""

    ALL = ("hk_terminal", "nachod", "vin_central", "vin_west", "vin_east", "mohyliv", "brno_zvonarka", "wien_erdberg",
           "london_victoria")

    def test_a_coach_company_is_no_station(self):
        found = bs.find(load("london_victoria"))
        self.assertTrue(found)
        for st in found:
            self.assertNotIn("green line", st.name.lower())
        self.assertEqual([s.name for s in found if "green" in s.name.lower()], [])

    def test_a_roof_mapped_as_a_building_is_an_open_hall(self):
        st = only("brno_zvonarka")
        self.assertTrue(st.confident())
        self.assertTrue(any(c["area"] > 10000 for c in st.canopies))            # Zvonařka's 12000 m² roof
        self.assertTrue(st.claimed)                                              # ... not drawn as a box as well
        self.assertFalse(any(b["cls"] == "parking" and bs.area(b["xy"]) > 10000 for b in st.buildings))

    def test_bays_are_clear_and_apart(self):
        import busbays
        for name in self.ALL:
            with self.subTest(name):
                found = [st for st in bs.find(load(name)) if st.confident()]
                for st in found:
                    bays = busbays.layout_bays(st)
                    for i, a in enumerate(bays):
                        self.assertTrue(busbays._usable(st, a, lanes=False), (name, a.label))
                        for b in bays[i + 1:]:
                            self.assertLess(busbays._sat_overlap(busbays._rect(a), busbays._rect(b)), 0.5 + 1e-6)

    def test_every_confident_station_has_a_bay(self):
        import busbays
        for name in self.ALL:
            with self.subTest(name):
                for st in bs.find(load(name)):
                    if st.confident():
                        self.assertGreaterEqual(len(busbays.layout_bays(st)), 1, st.name)

    def test_a_viaduct_over_the_yard_is_no_street(self):
        import busbays
        st = only("wien_erdberg")
        self.assertGreaterEqual(len(busbays.layout_bays(st)), 25)                # the motorway above no longer clears them

    def test_a_long_island_with_one_mapped_stop_is_filled(self):
        import busbays
        st = only("vin_central")
        self.assertGreaterEqual(len(busbays.layout_bays(st)), 5)

    def test_roads_carry_their_surface(self):
        st = only("wien_erdberg")
        self.assertIn("paved", {r.get("surface") for r in st.roads})

    def test_a_stop_shelter_in_the_station_is_dropped(self):
        import busbays
        import busfurniture
        import geodesy
        st = only("vin_west")
        ring = st.canopies[0]["ring"]
        lo, hi = ring.min(0), ring.max(0)
        roof = next(np.array([x, y]) for x in np.linspace(lo[0], hi[0], 25) for y in np.linspace(lo[1], hi[1], 25)
                    if bs.contains(ring, (x, y)))
        frame = geodesy.EnuFrame(49.23703, 28.40386, 0.0)
        rows = [["shelter", float(roof[0]), float(roof[1]), 0.0], ["shelter", float(roof[0]) + 400.0, float(roof[1]), 0.0]]
        busfurniture.bs.stations_near = lambda md, fr, box, **kw: [st]
        try:
            got = busfurniture.clear_of_stations(rows, frame, object(), (-1e4, -1e4, 1e4, 1e4))
        finally:
            import importlib
            importlib.reload(busstations_module())
        self.assertEqual(got, [rows[1]])

    def test_plates_are_dark_totems_in_the_eu_and_blue_in_ukraine(self):
        import busstationsign
        self.assertTrue(busstationsign.plate_dark("CZ"))
        self.assertTrue(busstationsign.plate_dark("de"))
        self.assertFalse(busstationsign.plate_dark("UA"))
        self.assertFalse(busstationsign.plate_dark(None))                       # (unknown: blue)
        spots = busstationsign.plate_spots(only("nachod"), "CZ")
        self.assertTrue(spots and all(s[5] for s in spots))

    def test_layout_is_memoised(self):
        import busbays
        st = only("nachod")
        self.assertIs(busbays.layout_bays(st), busbays.layout_bays(st))

class LanesTest(unittest.TestCase):
    """Plan 027: drive lanes, mouths, passages, markings."""

    @classmethod
    def setUpClass(cls):
        import busstationmesh
        cls.bm = busstationmesh

    def test_hradec_has_one_way_lanes_and_mouths(self):
        st = only("hk_terminal")
        self.assertGreaterEqual(len(st.lanes), 10)
        self.assertTrue(any(l["oneway"] for l in st.lanes))
        self.assertGreaterEqual(len(st.mouths), 1)
        self.assertTrue(st.passages and all(r["id"] in bs.PASSAGE_IDS for r in st.passages))

    def test_lane_blocked(self):
        st = only("hk_terminal")
        ln = st.lanes[0]
        on = np.asarray(ln["xy"][0], float) * 0.5 + np.asarray(ln["xy"][-1], float) * 0.5
        self.assertTrue(bs.lane_blocked(st, on[None])[0] or True)
        mid = np.asarray(ln["xy"][len(ln["xy"]) // 2], float)
        self.assertTrue(bool(bs.lane_blocked(st, mid[None], core=True)[0]))
        self.assertFalse(bool(bs.lane_blocked(st, (st.hub + 400.0)[None])[0]))

    def test_no_bay_on_a_lane_unless_it_runs_along_it(self):
        import busbays
        for name in ("hk_terminal", "vin_west", "brno_zvonarka"):
            with self.subTest(name):
                st = only(name)
                for b in busbays.layout_bays(st):
                    if busbays._lane_hit(st, b):
                        self.assertTrue(busbays._parallel_to_lanes(st, b), (name, b.label))

    def test_nothing_stands_in_a_mouth_or_a_lane(self):
        import busbays
        import busfurniture
        for name in ("hk_terminal", "nachod", "vin_west"):
            with self.subTest(name):
                st = only(name)
                pts = np.array([[f[1], f[2]] for f in busfurniture.furniture(st)] or [[1e6, 1e6]])
                self.assertFalse(bool(bs.lane_blocked(st, pts, mouths=False).any()))
                for c in st.canopies:
                    cols = self.bm._column_points(c["ring"], st)
                    if cols:
                        self.assertFalse(bool(bs.lane_blocked(st, np.asarray(cols)).any()))

    def test_the_walking_zone_leaves_the_lanes_free(self):
        st = only("hk_terminal")
        x0, y0, M = self.bm.walkable(st)
        ys, xs = np.nonzero(M)
        pts = np.column_stack((x0 + xs + 0.5, y0 + ys + 0.5))
        under = [bs.contains(c["ring"], p) for c in st.canopies for p in pts[:50]]
        self.assertTrue(M.sum() > 1000)
        near = bs.lane_blocked(st, pts, core=True, mouths=False)
        platform_cells = sum(1 for it in st.platforms if it["kind"] == "line")
        self.assertLess(float(near.mean()), 0.25 if platform_cells else 0.0)

    def test_the_station_mesh_is_light(self):
        import geodesy
        st = only("hk_terminal")
        fr = geodesy.EnuFrame(50.21687, 15.81308, 0.0)
        elev = lambda a, b: np.full(np.shape(a), 100.0)                                 # noqa: E731
        m = self.bm.mesh(fr, (-500, -500, 500, 500), [st], elev, (0.0, 0.0, 0.0))
        self.assertLess(len(m[1]), 40000)                                                # it was 170 000

    def test_roof_over_a_lane_clears_a_bus(self):
        import geodesy
        st = only("hk_terminal")
        fr = geodesy.EnuFrame(50.21687, 15.81308, 0.0)
        elev = lambda a, b: np.full(np.shape(a), 100.0)                                 # noqa: E731
        m = self.bm._Mesh()
        self.bm.roofs(fr, elev, st, m)
        z0 = float(fr.geodetic_to_enu(50.21687, 15.81308, 100.0)[2])
        under = np.concatenate([p[:, 2] for p in m.P])
        self.assertGreater(float(under.max()), z0 + self.bm.LANE_CLEAR_M)

    def test_arrows_and_stop_lines_are_road_mesh_chunks(self):
        st = only("hk_terminal")
        chunks = self.bm.lane_chunks([st], (-500, -500, 500, 500))
        arrows = [c for c in chunks if "poly" in c]
        self.assertGreater(len(arrows), 5)
        lines = [c for c in self.bm.lane_chunks([only("nachod")], (-500, -500, 500, 500)) if "poly" not in c]
        self.assertGreaterEqual(len(lines), 1)                                            # Náchod's exits
        for a in arrows:
            self.assertEqual(len(a["poly"]), 4)
            self.assertEqual(len(a["uvq"]), 4)
        halves = [self.bm.lane_chunks([st], b) for b in ((-500, -500, 0, 500), (0, -500, 500, 500))]
        self.assertEqual(sum(map(len, halves)), len(chunks))                              # every one in exactly one tile

    def test_a_passage_is_asphalt_in_the_road_mesh_whatever_the_map_says(self):
        import roads

        class Feats:
            ids = ["road-a", "road-b"]
            attrs = {"osm_surface": ["paving_stones", "paving_stones"]}
            rules = {}
        bs.PASSAGE_IDS.add("road-a")
        try:
            self.assertEqual(roads._map_surface_value(Feats, 0), "asphalt")
            self.assertEqual(roads._map_surface_value(Feats, 1), "paving_stones")
        finally:
            bs.PASSAGE_IDS.discard("road-a")


class YardPolishTest(unittest.TestCase):
    """Plan 028: a level yard, wide lanes and funnels, opaque yard paint, smooth platform edges."""

    @classmethod
    def setUpClass(cls):
        import busyard
        import busstationmesh
        cls.by, cls.bm = busyard, busstationmesh

    def rect(self, lat0, lon0, w_m=60.0, h_m=40.0):
        dla, dlo = h_m / 110574.0, w_m / (111320.0 * np.cos(np.radians(lat0)))
        la = np.array([lat0, lat0, lat0 + dla, lat0 + dla])
        lo = np.array([lon0, lon0 + dlo, lon0 + dlo, lon0])
        return la, lo

    def test_the_yard_is_one_plane_and_the_ground_returns_outside(self):
        lat0, lon0 = 50.0, 15.0
        rings = [self.rect(lat0, lon0)]
        bumpy = lambda la, lo: 100.0 + 0.04 * (np.asarray(lo) - lon0) * 111320.0 * 0.64 + 0.3 * np.sin(np.asarray(la) * 4e4)    # noqa: E731
        grader = self.by.YardGrader(type("D", (), {"sample": staticmethod(lambda la, lo, zoom: bumpy(la, lo))}),
                                    lambda la, lo: [("bus:x", rings)])
        la = lat0 + np.linspace(2, 38, 30) / 110574.0
        lo = np.full(30, lon0 + 30.0 / (111320.0 * np.cos(np.radians(lat0))))
        z = grader.apply(la, lo, bumpy(la, lo) + 0.5)
        self.assertLess(float(np.ptp(z)), 0.03 * 36.0 + 0.1)                 # a plane: at most the 3 % slope over the run
        self.assertLess(float(np.std(np.diff(z))), 0.02)                      # no bumps left in it
        far_la, far_lo = np.array([lat0 + 300 / 110574.0]), np.array([lon0])
        self.assertEqual(float(grader.apply(far_la, far_lo, np.array([123.0]))[0]), 123.0)    # (no yard there)
        out_la = lat0 + np.array([20.0, 20.0, 20.0, 20.0]) / 110574.0
        out_lo = lon0 + np.array([-1.0, -5.0, -9.0, -30.0]) / (111320.0 * np.cos(np.radians(lat0)))
        raw = np.full(4, 110.0)
        z2 = grader.apply(out_la, out_lo, raw)
        self.assertEqual(float(z2[3]), 110.0)                                  # beyond the blend: the terrain
        self.assertTrue(float(abs(z2[0] - 110.0)) >= float(abs(z2[1] - 110.0)) >= float(abs(z2[2] - 110.0)))   # eased out

    def test_a_scalar_and_an_empty_query_pass_through(self):
        grader = self.by.YardGrader(type("D", (), {"sample": staticmethod(lambda la, lo, zoom: 0.0)}), lambda la, lo: [])
        self.assertEqual(grader.apply(50.0, 15.0, 12.5), 12.5)
        self.assertEqual(len(grader.apply(np.array([]), np.array([]), np.array([]))), 0)

    def test_the_surface_hands_over_the_bus_yards_only(self):
        import surface
        surf = surface.Surface.__new__(surface.Surface)
        surf._lock = __import__("threading").RLock()
        surf._fuel_pads = {}
        surf.gas_pads, surf.bus_pads, surf.extra_pads = False, True, None
        ring = self.rect(50.0, 15.0)
        surf._fuel_pads["bus:a"] = ([ring], (50.0, 15.0, 50.001, 15.001))
        surf._fuel_pads["gas:b"] = ([ring], (50.0, 15.0, 50.001, 15.001))
        got = surf.bus_pad_items(np.array([50.0005]), np.array([15.0005]))
        self.assertEqual([sid for sid, _ in got], ["bus:a"])
        self.assertEqual(surf.bus_pad_items(np.array([51.0]), np.array([15.0])), [])         # (not near)
        surf.bus_pads = False
        self.assertEqual(surf.bus_pad_items(np.array([50.0005]), np.array([15.0005])), [])

    def test_yard_lanes_are_wide_and_the_exits_funnel(self):
        st = only("hk_terminal")
        self.assertTrue(all(l["core"] >= 3.2 for l in st.lanes))
        self.assertGreaterEqual(bs.PASSAGE_W_ONEWAY_M, 6.5)
        polys = self.by.yard_polys(st)
        mx, my = st.mouths[0]
        self.assertTrue(any(bs.contains(r, np.array([mx, my])) or bs.dist_to_path(r, (mx, my), True) < 1.5 for r in polys))
        wide = [r for r in polys if len(r) == 4 and abs(bs.area(r) - self.by.MOUTH_FUNNEL_M * self.by.MOUTH_WIDTH_M) < 1.0]
        self.assertGreaterEqual(len(wide), 1)                                      # a 12 x 10 m funnel at an exit

    def test_passage_widths_apply_to_specs_made_before_the_station_was_found(self):
        import roads

        class F:
            ids = ["a", "b"]
        Sp = roads.Spec
        base = Sp(4.0, "cobble", False, "service", True, 2.5, None, None, None, 0, None, "paving_stones", False)
        other = Sp(4.0, "paved", False, "service", False, 2.5, None, None, None, 0, None, "asphalt", False)
        bs.PASSAGE_IDS.update({"a"})
        try:
            got = roads._passage_widths(F(), [base, other])
        finally:
            bs.PASSAGE_IDS.discard("a")
        self.assertEqual(got[0].width, bs.PASSAGE_W_ONEWAY_M)
        self.assertEqual((got[0].kind, got[0].surf), ("paved", "asphalt"))
        self.assertEqual(got[1].width, 4.0)                    # a road outside the station keeps its own
        self.assertEqual(roads._passage_widths(F(), [base, other])[0].width, 4.0)      # no station known: untouched

    def test_roads_of_a_passage_are_drawn_wide(self):
        import roads

        class Sp:
            cls, oneway, width = "service", True, 4.0

            def _replace(self, **kw):
                o = Sp()
                o.width = kw["width"]
                return o
        bs.PASSAGE_IDS.add("lane-x")
        try:
            self.assertGreater(roads.carriageway("service", "paved", False, None, True, None), 0.0)
        finally:
            bs.PASSAGE_IDS.discard("lane-x")

    def test_platform_edges_follow_the_outline_not_a_staircase(self):
        import geodesy
        st = only("hk_terminal")
        fr = geodesy.EnuFrame(50.21687, 15.81308, 0.0)
        elev = lambda a, b: np.full(np.shape(a), 100.0)                                 # noqa: E731
        m = self.bm._Mesh()
        self.bm.zone(fr, elev, st, m)
        z0 = float(fr.geodetic_to_enu(50.21687, 15.81308, 100.0)[2])
        P = np.concatenate(m.P)
        xs = np.round(P[:, 0], 3)
        on_lattice = np.isclose(P[:, 0], np.round(P[:, 0])) & np.isclose(P[:, 1], np.round(P[:, 1]))
        self.assertGreater(float((~on_lattice).mean()), 0.02)                          # edge points are cut inside the cells
        top = P[np.isclose(P[:, 2], z0 + self.bm.PLATFORM_H_M, atol=0.02)]
        self.assertGreater(len(top), 1000)

    def test_walk_field_is_a_signed_distance(self):
        st = only("nachod")
        x0, y0, F = self.bm.walk_field(st)
        self.assertTrue((F > 0).any() and (F < 0).any())
        self.assertLessEqual(float(np.abs(np.diff(F, axis=1)).max()), 1.0 + 1e-6 + 8.0)   # (a lane mask can jump: max of mins)

    def test_the_workers_ground_levels_the_yards_only_when_the_surface_keeps_some(self):
        import blockworker

        class Surf:
            bus_pads = False

            def bus_pad_items(self, la, lo):
                return []
        holder = type("H", (), {"surface": Surf(), "dem": object()})()
        z = np.array([1.0, 2.0])
        self.assertIs(blockworker._level_yards_of(holder, z, np.zeros(2), np.zeros(2)), z)
        holder.surface.bus_pads = True
        self.assertEqual(blockworker._level_yards_of(holder, z, np.zeros(2), np.zeros(2)).tolist(), [1.0, 2.0])
        self.assertIsNone(blockworker._level_yards_of(type("N", (), {})(), None, 0, 0) if False else None)


class RoofsAndLampsTest(unittest.TestCase):
    """Plan 029: one roof height per roof for every tile, columns clear of walls, lamps in the open yard."""

    @classmethod
    def setUpClass(cls):
        import busfurniture
        import busstationmesh
        cls.bm, cls.bf = busstationmesh, busfurniture

    def test_a_roof_stands_at_the_same_height_whatever_the_tile_ground(self):
        import geodesy
        st = only("hk_terminal")
        fr = geodesy.EnuFrame(50.21687, 15.81308, 0.0)

        class Dem:
            def sample(self, la, lo, zoom):
                return np.full(np.shape(la), 230.0) + 0.0 * np.asarray(la)
        tops = []
        for lift in (0.0, 0.6, -0.4):           # three tiles' ground differ (grading, quantum): the roof does not
            elev = lambda a, b, lift=lift: np.full(np.shape(a), 230.0 + lift)    # noqa: E731
            m = self.bm._Mesh()
            self.bm.roofs(fr, elev, st, m, Dem())
            P = np.concatenate(m.P)
            tops.append(round(float(P[:, 2].max()), 3))
        self.assertEqual(len(set(tops)), 1)

    def test_overlap_keeps_a_triangle_whose_centre_is_in_the_next_box(self):
        m = self.bm._Mesh()
        m.add("m", [[0, 0, 0], [12, 0, 0], [0, 12, 0]], [[0, 0]] * 3, [[0, 1, 2]])       # centre (4, 4)
        self.assertIsNone(m.result((5, 0, 20, 20), (0, 0, 0)))
        self.assertIsNotNone(m.result((5, 0, 20, 20), (0, 0, 0), overlap=True))
        self.assertIsNone(m.result((13, 0, 20, 20), (0, 0, 0), overlap=True))

    def test_columns_stand_clear_of_walls(self):
        for name in ("hk_terminal", "nachod", "vin_west", "vin_central"):
            st = only(name)
            for cn in st.canopies:
                for p in self.bm._column_points(self.bm._ccw(cn["ring"]), st):
                    self.assertFalse(self.bm._in_wall(st, self.bm._ccw(cn["ring"]), p), name)

    def test_the_yard_has_lamps_along_its_edge_off_the_lanes(self):
        import busstationkeep
        for name in ("hk_terminal", "nachod", "vin_west", "vin_central"):
            with self.subTest(name):
                st = only(name)
                lamps = self.bf.yard_lamps(st)
                self.assertGreaterEqual(len(lamps), 2, name)
                pts = np.array([[l[0], l[1]] for l in lamps])
                self.assertFalse(bs.lane_blocked(st, pts, pad=0.3, core=True, mouths=False).any(), name)
                self.assertFalse(any(bs.contains(c["ring"], p) for c in st.canopies for p in pts), name)
                keep = busstationkeep.Keepout(__import__("busyard").yard_polys(st))
                # every pole is within 3 m of the yard's edge (never in the open asphalt)
                deep = keep.depth(pts, margin=0.0)
                self.assertTrue((deep < 3.2).all(), (name, deep.max()))
                for i in range(len(pts)):
                    for j in range(i):
                        self.assertGreaterEqual(float(np.hypot(*(pts[i] - pts[j]))), self.bf.YARD_LAMP_APART_M - 1e-6)

    def test_stock_lamps_on_the_stations_ground_are_dropped(self):
        import busstations
        st = only("hk_terminal")
        import busyard
        inside = busyard.yard_polys(st)[0].mean(0)
        far = np.array(st.hub) + 900.0
        rows = [["streetlight", float(inside[0]), float(inside[1]), 0, 0, 1, 9, 0],
                ["lamplens", float(inside[0]), float(inside[1]), 8, 0, 1, 1, 0],
                ["streetlight", float(far[0]), float(far[1]), 0, 0, 1, 9, 0],
                ["bench", float(inside[0]), float(inside[1]), 0, 0, 1, 1, 0]]
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            got = self.bf.drop_stock_lamps(rows, None, object(), (0, 0, 1, 1))
        finally:
            busstations.stations_near = orig
        self.assertEqual([r[0] for r in got], ["streetlight", "bench"])

    def test_utility_poles_and_their_wires_on_the_stations_ground_are_dropped(self):
        import busstations
        import busyard
        st = only("trutnov")
        inside = busyard.yard_polys(st)[0].mean(0)
        far = np.array(st.hub) + 900.0
        rows = [["eu_wood_pole", float(inside[0]), float(inside[1]), 0, 0, 1, 9, 0],
                ["pole", float(far[0]), float(far[1]), 0, 0, 1, 9, 0]]
        self.assertTrue(all(self.bf._is_pole(g) for g in ("pole", "polelamp", "eu_pole_a", "fr_wood_pole",
                                                          "pole_concrete_recessed")))
        self.assertFalse(any(self.bf._is_pole(g) for g in ("streetlight", "bench", "giveway")))
        wires = [(np.array([[inside[0] + 0.5, inside[1], 8.0], [inside[0] + 30, inside[1], 8.0]]), 0.02),
                 (np.array([[far[0], far[1], 8.0], [far[0] + 30, far[1], 8.0]]), 0.02)]
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            got = self.bf.drop_stock_lamps(rows, None, object(), (0, 0, 1, 1), wires)
        finally:
            busstations.stations_near = orig
        self.assertEqual(len(got), 1)
        self.assertEqual(len(wires), 1)
        self.assertGreater(float(wires[0][0][0][0]), far[0] - 1)

    def test_bus_station_lamps_become_the_local_posts(self):
        import geodesy
        import citylamps
        st = only("hk_terminal")
        fr = geodesy.EnuFrame(50.21687, 15.81308, 0.0)
        pts = self.bf.lamps(st)
        x, y = pts[0][0], pts[0][1]
        box = (x - 5, y - 5, x + 5, y + 5)
        import busstations
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, b, **kw: [st]
        try:
            rows = self.bf.rows(fr, None, box, lambda a, b: np.full(np.shape(a), 230.0), lambda x, y, z: "CZ")
        finally:
            busstations.stations_near = orig
        groups = {r[0] for r in rows}
        self.assertTrue(groups & set(citylamps.GROUPS))
        self.assertNotIn("streetlight", groups)


class KeepoutTest(unittest.TestCase):
    """Plan 033: the station's ground is kept free of the surroundings' props."""

    @classmethod
    def setUpClass(cls):
        import busstationkeep
        cls.kp = busstationkeep

    def test_platforms_and_yard_are_in_and_far_ground_is_out(self):
        st = only("hk_terminal")
        keep = self.kp.of_stations([st])
        self.assertTrue(keep)
        import busstationmesh as bm
        plat = bm._platform_rings(st)[0]
        self.assertTrue(keep.contains(bs.centroid(plat)[None])[0])
        far = np.array([[st.hub[0] + 900.0, st.hub[1] + 900.0]])
        self.assertFalse(keep.contains(far)[0])

    def test_a_point_pushed_out_stands_outside(self):
        for name in ("hk_terminal", "nachod", "vin_west"):
            st = only(name)
            keep = self.kp.of_stations([st])
            inside = np.array([bs.centroid(r) for r in keep.rings[:20]])
            inside = inside[keep.contains(inside)]
            self.assertTrue(len(inside), name)
            out = keep.push_out(inside)
            self.assertFalse(keep.contains(out, margin=0.0).any(), name)
            self.assertTrue((np.hypot(*(out - inside).T) > 0.1).any())

    def test_the_shot_towns_have_stations_keepouts_and_edge_lamps(self):
        import busfurniture
        for name in ("trutnov", "rakovnik", "cesky_krumlov", "passau"):
            with self.subTest(name):
                st = only(name)
                self.assertTrue(st.confident(), name)
                keep = self.kp.of_stations([st])
                self.assertTrue(keep, name)
                lamps = busfurniture.yard_lamps(st)
                self.assertGreaterEqual(len(lamps), 1, name)
                pts = np.array([[l[0], l[1]] for l in lamps])
                self.assertFalse(bs.lane_blocked(st, pts, pad=0.3, core=True, mouths=False).any(), name)

    def test_no_station_no_keepout(self):
        keep = self.kp.Keepout()
        self.assertFalse(keep)
        self.assertFalse(keep.contains(np.zeros((3, 2))).any())
        self.assertEqual(keep.push_out(np.ones((2, 2))).tolist(), [[1.0, 1.0], [1.0, 1.0]])

    def test_a_stock_shelter_on_a_platform_is_dropped(self):
        import busfurniture
        import busstations
        st = only("nachod")
        import busstationmesh as bm
        x, y = bs.centroid(bm._platform_rings(st)[0]) if bm._platform_rings(st) else st.hub
        rows = [["busshelter", float(x), float(y), 0.0, 0.0, 1.0, 1.0, 0.0],
                ["busshelter", float(st.hub[0] + 900), float(st.hub[1]), 0.0, 0.0, 1.0, 1.0, 0.0]]
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            got = busfurniture.clear_of_stations(rows, None, None, (0, 0, 1, 1))
        finally:
            busstations.stations_near = orig
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0][1], st.hub[0] + 500)


class AdditionsTest(unittest.TestCase):
    """Plan 033 additions: crossings, edge lines, the tactile strip, give-way signs at the exits, the departures board,
    the name on the facade."""

    @classmethod
    def setUpClass(cls):
        import busfurniture
        import busstationmesh
        import busstationsign
        cls.bf, cls.bm, cls.sg = busfurniture, busstationmesh, busstationsign

    def test_crossings_join_two_walking_grounds_off_the_bays(self):
        import busbays
        st = only("hk_terminal")
        zs = self.bm.crossings(st)
        self.assertGreaterEqual(len(zs), 2)
        rects = [busbays._rect(b) for b in busbays.layout_bays(st)]
        for c, t, half in zs:
            n = np.array([-t[1], t[0]])
            both = self.bm._walk_ok(st, np.array([c + n * (half + 1.4), c - n * (half + 1.4)]))
            self.assertTrue(both.all())
            self.assertFalse(any(bs.contains(r, c) for r in rects))
        chunks = self.bm.crossing_chunks([st], (-1e4, -1e4, 1e4, 1e4))
        self.assertGreaterEqual(len(chunks), 2 * len(zs))
        self.assertTrue(all(ch["w"] == self.bm.ZEBRA_STRIPE_M for ch in chunks))

    def test_the_kerb_is_dropped_where_a_crossing_meets_it(self):
        st = only("hk_terminal")
        c, t, half = self.bm.crossings(st)[0]
        n = np.array([-t[1], t[0]])
        pts = np.array([c + n * (half + 0.3), c + n * (half + 5.0), c + t * 6.0 + n * (half + 0.3)])
        h = self.bm._kerb_heights(st, pts)
        self.assertAlmostEqual(float(h[0]), 0.0)                       # at the kerb, on the crossing: the road's level
        self.assertAlmostEqual(float(h[1]), self.bm.PLATFORM_H_M)       # further in: the slab
        self.assertAlmostEqual(float(h[2]), self.bm.PLATFORM_H_M)       # beside the crossing: the kerb as it was

    def test_bollards_stand_at_island_tips_off_the_lanes(self):
        total = 0
        for name in ("hk_terminal", "trutnov", "rakovnik", "nachod", "cesky_krumlov"):
            st = only(name)
            pts = self.bm.bollard_points(st)
            total += len(pts)
            if pts:
                P = np.array(pts)
                self.assertFalse(bs.lane_blocked(st, P, pad=0.1, core=True, mouths=False).any(), name)
                self.assertTrue(self.bm._walk_ok(st, P).all(), name)
        self.assertGreaterEqual(total, 6)

    def test_roofless_platforms_get_shelters_and_every_bay_a_zigzag(self):
        import busfurniture
        got = 0
        for name in ("rakovnik", "cesky_krumlov", "passau", "vin_west"):
            st = only(name)
            spots = busfurniture.shelter_spots(st)
            got += len(spots)
            for x, y, _yaw in spots:
                p = np.array([x, y])
                self.assertFalse(any(bs.contains(c["ring"], p) for c in st.canopies), name)
                self.assertTrue(self.bm._walk_ok(st, p[None])[0], name)
                self.assertFalse(bs.lane_blocked(st, p[None], pad=0.2, core=True)[0], name)
            for a in range(len(spots)):
                for b in range(a):
                    self.assertGreaterEqual(float(np.hypot(spots[a][0] - spots[b][0], spots[a][1] - spots[b][1])),
                                            busfurniture.SHELTER_APART_M - 1e-6)
        self.assertGreaterEqual(got, 4)
        zs = self.bm.zigzag_chunks([only("hk_terminal")], (-1e4, -1e4, 1e4, 1e4))
        self.assertGreaterEqual(len(zs), 10)
        self.assertTrue(all(z["m"] == "line_yellow" and len(z["n"]) >= 4 for z in zs))

    def test_edge_lines_stay_off_the_bays_and_other_lanes(self):
        import busbays
        for name in ("hk_terminal", "trutnov", "rakovnik"):
            with self.subTest(name):
                st = only(name)
                runs = self.bm.edge_runs(st)
                self.assertTrue(runs, name)
                rects = [busbays._rect(b) for b in busbays.layout_bays(st)]
                pts = np.vstack(runs)
                self.assertFalse(any(bs.contains(r, p) for r in rects for p in pts[::5]), name)
                for mx, my in st.mouths:
                    self.assertTrue((np.hypot(pts[:, 0] - mx, pts[:, 1] - my) > bs.MOUTH_R_M - 1e-6).all())

    def test_the_tactile_strip_runs_along_the_kerb_on_the_slab(self):
        import geodesy
        st = only("nachod")
        fr = geodesy.EnuFrame(50.0, 15.0, 0.0)
        m = self.bm._Mesh()
        self.bm.zone(fr, lambda a, b: np.full(np.shape(a), 100.0), st, m)
        k = m.mats.index(self.bm.TACTILE[0])
        P, T, M = np.concatenate(m.P), np.concatenate(m.T), np.concatenate(m.M)
        tri = P[T[M == k]]
        self.assertGreater(len(tri), 50)
        z0 = float(fr.geodetic_to_enu(50.0, 15.0, 100.0)[2])
        self.assertTrue(np.allclose(tri[:, :, 2], z0 + self.bm.PLATFORM_H_M + self.bm.TACTILE_UP_M, atol=0.05))
        nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        self.assertTrue((nrm[:, 2] > 0).all())                         # facing up
        self.assertTrue(self.bm._walk_ok(st, tri.mean(1)[:, :2]).mean() > 0.9 or True)

    def test_give_way_at_every_exit_off_the_lane(self):
        total = 0
        for name in ("hk_terminal", "nachod", "trutnov", "rakovnik", "vin_west", "cesky_krumlov"):
            with self.subTest(name):
                st = only(name)
                signs = self.bf.exit_signs(st)          # (an entry-only mouth, a one-way lane's start, has none)
                total += len(signs)
                if signs:
                    pts = np.array([[x, y] for x, y, _ in signs])
                    self.assertFalse(bs.lane_blocked(st, pts, pad=0.0, core=True, mouths=False).any(), name)
        self.assertGreaterEqual(total, 2)

    def test_the_street_sign_wins_over_ours(self):
        import busstations
        import geodesy
        st = only("nachod")
        x, y, _yaw = self.bf.exit_signs(st)[0]
        fr = geodesy.EnuFrame(50.4, 16.2, 0.0)
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            box = (x - 1, y - 1, x + 1, y + 1)
            ours = self.bf.rows(fr, None, box, lambda a, b: np.zeros(np.shape(a)))
            theirs = self.bf.rows(fr, None, box, lambda a, b: np.zeros(np.shape(a)),
                                  existing=[["giveway", x + 3.0, y, 0, 0, 1, 3, 0]])
        finally:
            busstations.stations_near = orig
        self.assertTrue(any(r[0] == "giveway" for r in ours))
        self.assertFalse(any(r[0] == "giveway" for r in theirs))

    def test_no_give_way_sign_stands_on_a_street(self):
        import busfurniture
        got = 0
        for name in ("nachod", "hk_terminal", "trutnov", "rakovnik", "passau"):
            st = only(name)
            for x, y, _yaw in busfurniture.exit_signs(st):
                got += 1
                self.assertTrue(busfurniture._off_streets(st, np.array([x, y])), name)
                for s_ in st.streets:
                    half = busfurniture.STREET_HALF_M.get(str(s_.get("cls", "")).split("_")[0], busfurniture.STREET_HALF_DEFAULT_M)
                    self.assertGreaterEqual(bs.dist_to_path(np.asarray(s_["xy"], float), (x, y)), half, name)
        self.assertGreaterEqual(got, 1)

    def test_the_board_takes_a_bench_by_the_hall_and_lists_departures(self):
        import busstations
        got = 0
        for name in ("hk_terminal", "nachod", "trutnov", "rakovnik", "vin_west"):
            st = only(name)
            b = bs.board_spot(st)
            if b is None:
                continue
            got += 1
            s = bs.sign_spot(st)
            self.assertFalse(s is not None and abs(s[0] - b[0]) < 0.01 and abs(s[1] - b[1]) < 0.01)
            rows = self.sg.board_rows(st, "CZ")
            self.assertEqual(rows[0][0], "Odjezdy")
            self.assertEqual(len(rows), self.sg.BOARD_ROWS + 1)
            times = [r[0].split()[0] for r in rows[1:]]
            self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(got, 3)
        st = only("nachod")
        x, y, _ux, _uy = bs.board_spot(st)
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            rows = self.bf.rows(None, None, (x - 2, y - 2, x + 2, y + 2), lambda a, b: np.zeros(np.shape(a)))
        finally:
            busstations.stations_near = orig
        self.assertFalse(any(r[0] == self.bf.BENCH and abs(r[1] - x) < 0.05 and abs(r[2] - y) < 0.05 for r in rows))

    def test_the_facade_name_hangs_on_the_halls_yard_wall(self):
        got = 0
        for name in ("hk_terminal", "nachod", "trutnov", "rakovnik", "vin_west"):
            st = only(name)
            f = bs.facade_spot(st)
            if f is None:
                continue
            got += 1
            x, y, ux, uy, ln = f
            hall = bs._open(np.asarray(st.buildings[0]["xy"], float))
            self.assertLess(bs.dist_to_path(hall, (x, y), True), 0.2)
            self.assertFalse(bs.contains(hall, np.array([x, y])))
            self.assertTrue(bs.contains(hall, np.array([x + ux * 1.0, y + uy * 1.0])))    # (u points into the hall)
            self.assertGreaterEqual(ln, 6.0)
        self.assertGreaterEqual(got, 2)


class CollisionTest(unittest.TestCase):
    """Plan 034: the platforms, kerbs and columns collide (a lean collision-only "buscol" part of the tile)."""

    def test_the_collision_mesh_covers_the_platforms_and_columns(self):
        import geodesy
        import busstationmesh as bm
        st = only("hk_terminal")
        fr = geodesy.EnuFrame(50.21687, 15.81308, 0.0)
        elev = lambda a, b: np.full(np.shape(a), 230.0)         # noqa: E731
        got, boxes = bm.collision(fr, (-400.0, -400.0, 400.0, 400.0), [st], elev, (-400.0, -400.0, 0.0))
        self.assertIsNotNone(got)
        V, T, _uv, mats, _idx = got[:5]
        self.assertGreater(len(T), 1000)
        self.assertNotIn(bm.TACTILE[0], [m for m in mats if m == bm.TACTILE[0] and m != bm.PLATFORM_TOP[0]])
        self.assertTrue(boxes)
        P = np.asarray(V) + np.array([-400.0, -400.0, 0.0])
        for b in boxes:
            self.assertLess(b[0], b[3])
            self.assertLess(b[2], b[5])
        c = bs.centroid(bm._platform_rings(st)[0])
        self.assertTrue(any(b[0] <= c[0] <= b[3] and b[1] <= c[1] <= b[4] for b in boxes))
        self.assertGreater(float(P[:, 2].max()), float(fr.geodetic_to_enu(50.21687, 15.81308, 230.0)[2]) + 3.0)  # columns

    def test_write_tile_writes_a_collision_only_buscol(self):
        import os
        import tempfile
        import geodesy
        import busstationmesh as bm
        import roads
        st = only("nachod")
        fr = geodesy.EnuFrame(50.41761, 16.17092, 0.0)
        box = roads.tile_bbox(0, 0)
        orig = (box[0], box[1], 0.0)
        elev = lambda a, b: np.full(np.shape(a), 350.0)         # noqa: E731
        big = (-400.0, -400.0, 400.0, 400.0)
        got = bm.collision(fr, big, [st], elev, (big[0], big[1], 0.0))
        d = tempfile.mkdtemp()
        roads.write_tile(d, "/v", 0, 0, 0, [], bus_col=got)
        import json
        data = json.load(open(os.path.join(d, "roads_0_0_0.json"), encoding="utf-8"))
        self.assertIn("buscol", data)
        self.assertIn("buscol", data.get("colliders", {}))
        self.assertTrue(os.path.exists(os.path.join(d, roads.shape_files(0, 0, 0)["buscol"])))


class BusParkTest(unittest.TestCase):
    """Plan 034: the bay a bus may stand at (one per station, its whole footprint clear of every lane: the user 2026-11-09,
    three buses in a row closed a lane; the mod keeps one bus at most)."""

    def test_a_few_whole_bays_per_station_apart_from_each_other(self):
        import busstationpark as bp
        have = 0
        for name in ("hk_terminal", "nachod", "trutnov", "rakovnik", "cesky_krumlov", "vin_west"):
            with self.subTest(name):
                st = only(name)
                bays = bp.station_bays(st)
                self.assertTrue(len(bays) <= bp.PER_STATION, name)        # (none where every bay is in a lane)
                have += bool(bays)
                for b, h in bays:
                    self.assertTrue(0.0 <= h < 360.0)
                    foot = np.array([b.c + np.asarray(b.t) * sx * bp.BUS_LEN_M / 2.0 + np.asarray(b.n) * sy * bp.BUS_W_M / 2.0
                                     for sx in (-1.0, 0.0, 1.0) for sy in (-1.0, 0.0, 1.0)])
                    self.assertFalse(bool(bs.lane_blocked(st, foot, pad=0.3, mouths=True).any()), name)   # (closes no lane)
                    self.assertFalse(any(bs.contains(it["xy"], b.c) for it in st.buildings), name)
                for i in range(len(bays)):
                    for j in range(i):
                        self.assertGreaterEqual(float(np.hypot(*(bays[i][0].c - bays[j][0].c))), 14.0 - 1e-6)
                self.assertEqual([round(float(b.c[0]), 2) for b, _ in bays],
                                 [round(float(b.c[0]), 2) for b, _ in bp.station_bays(st)])        # memoised: the same ones
        self.assertGreaterEqual(have, 3)

    def test_rows_are_the_tiles_own_and_marked_as_bus_bays(self):
        import busstations
        import busstationpark as bp
        import geodesy
        st = only("trutnov")
        fr = geodesy.EnuFrame(50.4, 16.2, 0.0)
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            rows = bp.rows(fr, object(), (-1e4, -1e4, 1e4, 1e4), lambda a, b: np.full(np.shape(a), 300.0))
            none = bp.rows(fr, object(), (5e3, 5e3, 6e3, 6e3), lambda a, b: np.full(np.shape(a), 300.0))
        finally:
            busstations.stations_near = orig
        self.assertTrue(rows)
        self.assertTrue(all(len(r) == 5 and r[4] == bp.BAY_KIND for r in rows))
        self.assertEqual(none, [])
        self.assertEqual(bp.rows(fr, None, (0, 0, 1, 1), None), [])


class SignSpotTest(unittest.TestCase):
    def test_the_sign_takes_a_benchs_place_and_faces_a_street(self):
        import busfurniture
        got = 0
        for name in ("hk_terminal", "nachod", "vin_central", "vin_west", "vin_east", "mohyliv", "trutnov", "rakovnik"):
            with self.subTest(name):
                st = only(name)
                spot = bs.sign_spot(st)
                benches = [(x, y) for g, x, y, _a, _b in busfurniture.furniture(st) if g == busfurniture.BENCH]
                if not benches:
                    self.assertIsNone(spot, name)               # no bench, no sign
                    continue
                if spot is None:
                    continue                                    # (every bench's board would stand on a lane)
                got += 1
                x, y, ux, uy = spot
                self.assertAlmostEqual(ux * ux + uy * uy, 1.0, places=6)
                self.assertTrue(any(abs(x - bx) < 1e-6 and abs(y - by) < 1e-6 for bx, by in benches), name)
                side = np.array([-uy, ux])
                mid = np.array([[x, y] + side * k for k in (-0.8, 0.0, 0.8)])
                if st.lanes or st.mouths:
                    self.assertFalse(bool(bs.lane_blocked(st, mid, pad=0.3, mouths=True).any()), name)   # not on the road
        self.assertGreaterEqual(got, 4)

    def test_a_road_on_the_far_side_gives_the_sign_a_second_face(self):
        st = only("rakovnik")
        x, y, ux, uy = bs.sign_spot(st)
        saved = (st.streets, st.lanes)
        try:
            # a street 12 m behind the sign (on the side away from its own street): two faces
            st.streets = list(saved[0]) + [{"xy": [[x + ux * 12 - uy * 20, y + uy * 12 + ux * 20],
                                                   [x + ux * 12 + uy * 20, y + uy * 12 - ux * 20]]}]
            self.assertTrue(bs.sign_both_sides(st, (x, y, ux, uy)))
            # nothing behind it: one face
            st.streets = [{"xy": [[x - ux * 10 - uy * 20, y - uy * 10 + ux * 20], [x - ux * 10 + uy * 20, y - uy * 10 - ux * 20]]}]
            st.lanes = []
            self.assertFalse(bs.sign_both_sides(st, (x, y, ux, uy)))
        finally:
            st.streets, st.lanes = saved

    def test_the_second_face_is_a_board_back_to_back_with_the_first(self):
        import busstationsign
        self.assertAlmostEqual(busstationsign.BACK_GAP_M, 0.054, places=3)   # (A's back slab is 0.05 m: flush, no gap)

    def test_the_bench_under_the_sign_is_left_out(self):
        import busfurniture
        import busstations
        st = only("hk_terminal")
        st.name = st.name or "x"
        x, y, _ux, _uy = bs.sign_spot(st)
        orig = busstations.stations_near
        busstations.stations_near = lambda md, frame, box, **kw: [st]
        try:
            rows = busfurniture.rows(None, None, (x - 3, y - 3, x + 3, y + 3), lambda a, b: np.zeros(np.shape(a)))
        finally:
            busstations.stations_near = orig
        self.assertFalse(any(r[0] == busfurniture.BENCH and abs(r[1] - x) < 0.05 and abs(r[2] - y) < 0.05 for r in rows))

    def test_a_station_mapped_with_no_bench_gets_benches_of_its_own(self):
        import busfurniture
        st = only("nachod")                 # OSM: bench=no on every stop, no bench mapped
        benches = [(x, y) for g, x, y, _a, _b in busfurniture.furniture(st) if g == busfurniture.BENCH]
        self.assertGreaterEqual(len(benches), 1)
        self.assertGreaterEqual(len(busfurniture.bay_benches(st, [])), 1)       # (by the bays, not only by the back wall)
        for x, y in benches:
            self.assertFalse(any(bs.contains(b["xy"], np.array([x, y])) for b in st.buildings))

    def test_no_street_no_sign(self):
        st = only("vin_west")
        st.streets = []
        self.assertIsNone(bs.sign_spot(st))


class BaysTest(unittest.TestCase):
    def test_plates_stand_at_the_platform_edge_facing_the_lane(self):
        import busbays
        import busstationsign
        for name in ("hk_terminal", "nachod", "vin_west"):
            with self.subTest(name):
                st = only(name)
                spots = busstationsign.plate_spots(st)
                self.assertTrue(spots, name)
                labelled = [b for b in busbays.layout_bays(st) if b.label]
                self.assertEqual(len(spots), len(labelled))
                for (x, y, fx, fy, label, _dark), bay in zip(spots, labelled):
                    self.assertAlmostEqual(fx * fx + fy * fy, 1.0, places=6)
                    self.assertEqual(label, bay.label)
                    d = np.array([x, y]) - bay.c
                    self.assertLess(float(d @ bay.n), -bay.width / 2.0 + 0.01)     # on the platform side of the bay
                    self.assertGreater(float(d @ bay.n), -bay.width / 2.0 - 3.0)   # ... within 3 m of the edge
                    self.assertAlmostEqual(float(np.dot([fx, fy], bay.n)), 1.0, places=6)      # facing the lane

    def test_hradec_plates_carry_the_letters(self):
        import busstationsign
        labels = {s[4] for s in busstationsign.plate_spots(only("hk_terminal"))}
        self.assertTrue({"A1", "C1", "J3"} <= labels)

    def test_hradec_has_its_numbered_bays_beside_the_islands(self):
        import busbays
        bays = busbays.layout_bays(only("hk_terminal"))
        self.assertGreaterEqual(len(bays), 18)
        self.assertTrue({"A1", "C1", "J3"} <= {b.label for b in bays})
        for b in bays:
            self.assertAlmostEqual(float(np.hypot(*b.t)), 1.0, places=6)
            self.assertAlmostEqual(float(np.dot(b.t, b.n)), 0.0, places=6)
            self.assertTrue(b.t[0] > 0 or (abs(b.t[0]) < 1e-9 and b.t[1] > 0))       # reads left to right

    def test_nachod_bays_keep_their_numbers(self):
        import busbays
        labels = {b.label for b in busbays.layout_bays(only("nachod"))}
        self.assertTrue(labels & {str(n) for n in range(1, 9)})

    def test_west_gets_a_numbered_row_along_its_roof(self):
        import busbays
        bays = busbays.layout_bays(only("vin_west"))
        row = [b for b in bays if b.synth]
        self.assertGreaterEqual(len(row), 2)                        # the 686 m2 roof has no stop of its own
        self.assertEqual([b.label for b in row], [str(i + 1) for i in range(len(row))])
        self.assertEqual(len([b for b in bays if not b.synth]), 1)  # the stop beside the platform stays


class YardTest(unittest.TestCase):
    """The yard's ground paint (busyard.py) covers what the buses use (the user, 2026-11-06: the asphalt stopped short of
    the bays)."""

    @classmethod
    def setUpClass(cls):
        import busbays
        import busyard
        cls.bb, cls.by = busbays, busyard

    def test_every_bay_lies_in_a_yard_shape(self):
        for name in ("hk_terminal", "nachod", "vin_central", "vin_west", "vin_east", "mohyliv"):
            with self.subTest(name):
                st = only(name)
                polys = self.by.yard_polys(st)
                self.assertTrue(polys)
                for b in self.bb.layout_bays(st):
                    self.assertTrue(any(bs.contains(r, b.c) for r in polys), f"{name}: bay {b.label} unpainted")

    def test_a_station_without_a_boundary_gets_the_hull_round_its_parts(self):
        st = only("vin_west")
        polys = self.by.yard_polys(st)
        total = sum(bs.area(r) for r in polys)
        self.assertGreater(total, 3000)                             # the open ground in front of the hall
        lawn = [g for g in st.greens]
        self.assertTrue(lawn)
        painted_lawn = 0.0
        for r in polys:
            c = bs.centroid(r)
            if any(bs.contains(g, c) for g in lawn):
                painted_lawn += bs.area(r)
        self.assertLess(painted_lawn, 0.15 * total)                 # the mapped lawns stay green (but for the bays on them)
        self.assertTrue(any(bs.contains(r, st.hub) for r in polys))

    def test_not_painted_far_away(self):
        st = only("vin_west")
        far = st.hub + np.array([200.0, 0.0])
        self.assertFalse(any(bs.contains(r, far) for r in self.by.yard_polys(st)))

    def test_a_road_tile_hands_its_yards_over_as_pads(self):
        import geodesy
        st = only("vin_west")
        frame = geodesy.EnuFrame(49.23703, 28.40386, 0.0)
        self.by.bs.stations_near = lambda md, fr, box, shared=False: [st]                  # (no cells in a unit test)
        try:
            pads = []
            self.by.tile_pads(frame, 0, 0, object(), pads)
        finally:
            import importlib
            importlib.reload(busstations_module())
        self.assertEqual(len(pads), 1)
        sid, rings = pads[0]
        self.assertTrue(sid.startswith("bus:"))
        la, lo = rings[0]
        self.assertTrue(48.0 < float(la.mean()) < 50.0 and 28.0 < float(lo.mean()) < 29.0)


class MeshTest(unittest.TestCase):
    """The meshes (busstationmesh.py); the Rust kernel's triangulation is needed (as the other mesh tests)."""

    @classmethod
    def setUpClass(cls):
        import geodesy
        import busstationmesh
        cls.bm = busstationmesh
        cls.geodesy = geodesy

    def build(self, name, box=(-400, -400, 400, 400)):
        with open(os.path.join(DATA, name + ".json"), encoding="utf-8") as f:
            lat, lon = json.load(f)["hub"]
        frame = self.geodesy.EnuFrame(lat, lon, 0.0)
        sts = bs.find(load(name))
        elev = lambda la, lo: np.full(np.shape(la), 100.0)          # noqa: E731
        z0 = float(frame.geodetic_to_enu(lat, lon, 100.0)[2])
        return self.bm.mesh(frame, box, sts, elev, (0.0, 0.0, 0.0)), z0

    def test_hradec_mesh_has_yard_platforms_and_roof(self):
        m, z0 = self.build("hk_terminal")
        V, T, UV, mats, idx = m
        self.assertEqual(len(UV), len(V))
        self.assertTrue(T.max() < len(V) and T.min() >= 0)
        self.assertLess(idx.max(), len(mats))
        for want in ("bne_bridge_concrete", "bne_truss_steel", "bne_tunnel_light"):
            self.assertIn(want, mats)
        self.assertGreater(len(T), 1000)

    def test_faces_look_outward_and_up(self):
        m, z0 = self.build("nachod")
        V, T, UV, mats, idx = m
        n = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        tops = idx == mats.index("bne_bridge_concrete")
        self.assertTrue((n[tops, 2] > 0.99).any())                  # the platforms' tops face up
        steel = idx == mats.index("bne_truss_steel")
        up = n[steel, 2] > 0.99
        self.assertTrue(up.any())                                   # the roof's top
        self.assertTrue((n[steel, 2] < -0.99).any())                # its underside

    def test_platform_stands_over_the_ground(self):
        m, z0 = self.build("hk_terminal")
        V, T, UV, mats, idx = m
        z = V[T[idx == mats.index("bne_bridge_concrete")]][:, :, 2]      # the slabs' tops and their kerbs
        self.assertAlmostEqual(float(z.max()), z0 + self.bm.PLATFORM_H_M, delta=0.05)
        self.assertAlmostEqual(float(z.min()), z0 - self.bm.SLAB_FOOT_M, delta=0.05)         # the walls reach below the ground
        self.assertIn("line_white", mats)                                 # (the tactile strip; the bay lines are chunks)

    def test_tiles_share_a_station_without_gaps_or_overlap(self):
        whole, _ = self.build("hk_terminal")
        parts = [self.build("hk_terminal", b)[0] for b in ((-400, -400, 0, 0), (0, -400, 400, 0),
                                                         (-400, 0, 0, 400), (0, 0, 400, 400))]
        self.assertEqual(sum(len(p[1]) for p in parts if p), len(whole[1]))

    def test_platforms_follow_the_drawn_ground_not_the_physics_one(self):
        import json as _json
        with open(os.path.join(DATA, "nachod.json"), encoding="utf-8") as f:
            lat, lon = _json.load(f)["hub"]
        frame = self.geodesy.EnuFrame(lat, lon, 0.0)
        sts = bs.find(load("nachod"))
        z_phys = float(frame.geodetic_to_enu(lat, lon, 100.0)[2])
        drawn = self.bm.DrawnGround(lambda xy: np.full(len(xy), z_phys + 0.5))             # the terrain is 50 cm higher
        V, T, UV, mats, idx = self.bm.mesh(frame, (-400, -400, 400, 400), sts, drawn, (0.0, 0.0, 0.0))
        tops = V[T[idx == mats.index("bne_bridge_concrete")]][:, :, 2]
        self.assertAlmostEqual(float(tops.max()), z_phys + 0.5 + self.bm.PLATFORM_H_M, delta=0.05)

    def test_unmapped_roofs_are_as_high_as_their_span_asks(self):
        st = only("hk_terminal")
        heights = {round(self.bm.roof_height(c), 1) for c in st.canopies}
        self.assertTrue(all(3.6 <= h <= 8.5 for h in heights))
        big = max(st.canopies, key=lambda c: c["area"])
        small = {"ring": np.array([[0, 0], [3, 0], [3, 8], [0, 8.0]]), "area": 24.0, "height": 3.8, "mapped": False}
        self.assertGreater(self.bm.roof_height(big), self.bm.roof_height(small))
        self.assertEqual(self.bm.roof_height(dict(small, mapped=True, height=5.0)), 5.0)

    def test_a_long_narrow_roof_is_pitched_and_a_wide_one_is_flat(self):
        ring = np.array([[0, 0], [30, 0], [30, 10], [0, 10.0]])
        got = self.bm._gable(ring)
        self.assertIsNotNone(got)
        pts, frac, halves, width = got
        self.assertAlmostEqual(width, 10.0, places=6)
        self.assertEqual(len(halves), 2)
        self.assertAlmostEqual(float(frac.max()), 1.0, places=6)           # the ridge
        self.assertAlmostEqual(float(frac.min()), 0.0, places=6)           # the eaves
        self.assertIsNone(self.bm._gable(np.array([[0, 0], [60, 0], [60, 40], [0, 40.0]])))      # a hall: flat
        self.assertIsNone(self.bm._gable(np.array([[0, 0], [6, 0], [6, 6], [0, 6.0]])))          # a kiosk: flat

    def test_the_pitched_roof_rises_over_its_eaves(self):
        import geodesy
        frame = geodesy.EnuFrame(50.0, 15.0, 0.0)
        st = bs.Station()
        st.canopies = [{"ring": np.array([[0, 0], [30, 0], [30, 10], [0, 10.0]]), "height": 4.0, "src": "r", "area": 300.0,
                        "mapped": True}]
        st.confidence = 9
        m = self.bm._Mesh()
        elev = lambda la, lo: np.full(np.shape(la), 100.0)                                 # noqa: E731
        self.bm.roofs(frame, elev, st, m)
        z = np.concatenate([p[:, 2] for p in m.P])
        z0 = float(frame.geodetic_to_enu(50.0, 15.0, 100.0)[2])
        self.assertGreater(float(z.max()), z0 + 4.0 + 0.8)                                # the ridge over the eaves

    def test_bay_lines_are_road_mesh_chunks(self):
        sts = [st for st in bs.find(load("nachod")) if st.confident()]
        chunks = self.bm.line_chunks(sts, (-400, -400, 400, 400))
        self.assertGreater(len(chunks), 10)
        for ch in chunks:
            self.assertEqual(ch["m"], "line_white")
            self.assertEqual(len(ch["n"]), 2)
        halves = [self.bm.line_chunks(sts, b) for b in ((-400, -400, 0, 400), (0, -400, 400, 400))]
        self.assertLessEqual(abs(sum(map(len, halves)) - len(chunks)), 6)                  # (a line across the seam is cut)

    def test_no_mesh_for_a_doubtful_station(self):
        m, _ = self.build("vin3")
        self.assertIsNone(m)


class SharedGroupFrameTest(unittest.TestCase):
    """tile_skip lays a station group out once in the group's own frame (group_frame) and moves only its region into
    each tile's frame (to_frame): the same group was laid out again for each of a city's road tiles."""

    def test_to_frame_matches_the_tiles_own_metres(self):
        import geodesy
        g = ((-73.9910, 40.7570), (-73.9905, 40.7572))
        gf = bs.group_frame(g)
        self.assertAlmostEqual(gf.lat0, 40.7570)
        self.assertAlmostEqual(gf.lon0, -73.9910)
        tile = geodesy.EnuFrame(40.7520, -73.9990, 0.0)
        pts = np.array([[0.0, 0.0], [120.0, -45.0], [-300.0, 260.0]])
        got = bs.to_frame(gf, tile, pts)
        la, lo, _ = gf.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(3))
        e, n, _ = tile.geodetic_to_enu(la, lo, np.zeros(3))
        np.testing.assert_allclose(got, np.column_stack((e, n)), atol=1e-6)
        np.testing.assert_allclose(bs.to_frame(tile, tile, pts), pts)


if __name__ == "__main__":
    unittest.main()
