"""Roads near the player (roads.py) and the road surfaces (surface.paint_roads): the class / surface rules,
the polyline helpers, the tile build (clipped, simplified, chunked, deterministic), the Tracker's
send / drop / anchor-change logic, and roads painted into the material grid."""
import asyncio
import json
import os
import sys
import tempfile
import math
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402
import surface as sf  # noqa: E402

LAT, LON = 39.2834, -76.5934
FRAME = geodesy.EnuFrame(LAT, LON, 0.0)
M_LAT = 111320.0
M_LON = 111320.0 * np.cos(np.radians(LAT))


def rules(entries, kind="text"):
    """RuleTable from [(feature, value[, start, end]), ...] (default: covering whole features)."""
    if not entries:
        return mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, "<U8"), np.zeros(0, np.float32), np.zeros(0, np.float32))
    f = np.array([e[0] for e in entries], np.int32)
    v = np.array([e[1] for e in entries], np.float32 if kind == "float" else "<U16")
    a = np.array([e[2] if len(e) > 2 else 0.0 for e in entries], np.float32)
    b = np.array([e[3] if len(e) > 3 else 1.0 for e in entries], np.float32)
    return mapgeom.RuleTable(f, v, a, b)


def features(lines, classes, surfaces=(), widths=(), levels=(), flags=(), ids=None, subtypes=None):
    """Features of LINE roads; lines: list of [(east m, north m), ...] around (LAT, LON)."""
    coords, part_off, ring_off, coord_off = [], [0], [0], [0]
    for ln in lines:
        for e, n in ln:
            coords.append((LON + e / M_LON, LAT + n / M_LAT))
        coord_off.append(len(coords))
        ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    n = len(lines)
    return mapgeom.Features(
        layer="roads", ids=np.array(ids or [f"id{i:03d}" for i in range(n)]), kind=np.full(n, mapgeom.LINE, np.uint8),
        part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
        coord_off=np.array(coord_off, np.int64), lonlat=np.array(coords, np.float64),
        attrs={"class": np.array(classes), "subtype": np.array(subtypes or ["road"] * n)},
        rules={"surface": rules(list(surfaces)), "width": rules(list(widths), "float"),
               "level": rules(list(levels), "float"), "flags": rules(list(flags))})


def flat(lat, lon):
    return np.full(np.shape(lat), 10.0)



class WalkLevelTest(unittest.TestCase):
    def test_sidewalk_stops_at_a_wall(self):
        """A sidewalk along y = 6 at the road's level 10 m: where the ground under it climbs a wall (x 30..50) it stops."""
        run = np.c_[np.arange(0.0, 81.0, 2.0), np.full(41, 6.0), np.full(41, 10.0)]

        def elev(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            return 10.0 + 0.3 * np.sin(e / 7.0) + np.where((e > 30) & (e < 50), 4.0, 0.0)
        pieces = roads._on_level(FRAME, elev, run)
        self.assertEqual(len(pieces), 2)
        self.assertLess(pieces[0][-1, 0], 31.0)
        self.assertGreater(pieces[1][0, 0], 49.0)
        self.assertEqual(len(roads._on_level(FRAME, lambda la, lo: np.full(np.shape(la), 10.4), run)), 1)

class RulesTest(unittest.TestCase):
    def test_surface_kind(self):
        self.assertEqual(roads.surface_kind("residential", None), "paved")
        self.assertEqual(roads.surface_kind("track", None), "dirt")
        self.assertEqual(roads.surface_kind("track", "asphalt"), "paved")
        self.assertEqual(roads.surface_kind("primary", "gravel"), "gravel")
        self.assertEqual(roads.surface_kind("primary", "unpaved"), "dirt")
        # the detail after a colon (or a second value after a semicolon) does not lose the base surface: İzmir's
        # sett streets were drawn as dirt/gravel tracks (2026-10-22)
        for v in ("cobblestone:flattened", "sett:pattern", "paving_stones:30", "sett;asphalt"):
            self.assertEqual(roads.surface_kind("residential", v), "cobble", v)
        self.assertEqual(roads.surface_kind("residential", "concrete:lanes"), "paved")
        # interlocking concrete pavers, under the names the map gives them
        for v in ("interlocking_pavers", "interlocking_stones"):
            self.assertEqual(roads.surface_kind("residential", v), "cobble", v)
        # the rock family and grass are unpaved, never a marked asphalt street (gridmap v2's Rock/Grass are
        # TerrainMaterials: a heightfield shader, not something a road decal can carry)
        for v in ("rock", "bedrock", "rocky", "grass", "grass_paver"):
            self.assertEqual(roads.surface_kind("residential", v), "dirt", v)
        for v in ("stone", "stones"):           # (OSM: mostly stone paving)
            self.assertEqual(roads.surface_kind("residential", v), "cobble", v)
        # a value not understood at all is the class's own surface, not the dirt road
        self.assertEqual(roads.surface_kind("residential", "whatever"), "paved")
        self.assertEqual(roads.surface_kind("track", "whatever"), "dirt")

    def test_surface_token(self):
        self.assertEqual(roads.surface_token("sett:pattern"), "sett")
        self.assertEqual(roads.surface_token("sett;asphalt"), "sett")
        self.assertEqual(roads.surface_token(" Concrete:Lanes "), "concrete")
        self.assertIsNone(roads.surface_token(None))

    def test_deferred_specs_keep_their_own_surface(self):
        # no mapped width + an urban sample: these go through the deferred (ask) path, which used to reuse the
        # surface token left over from the last feature scanned (Codex on PR #119)
        f = features([[(0, 0), (10, 0)]] * 2, ["residential", "residential"],
                     surfaces=[(0, "concrete"), (1, "asphalt")])
        s = roads.road_specs(f, urban_fn=lambda lat, lon: np.ones(len(lat), bool))
        self.assertEqual([sp.surf for sp in s], ["concrete", "asphalt"])

    def test_rock_and_grass_live_in_the_ground_layer(self):
        def spec(surf):
            return roads.Spec(6.0, "dirt", False, "residential", False, 2.5, 0, None, surf=surf)

        for v in ("rock", "rocky", "bedrock"):
            self.assertEqual(roads.terrain_surface(spec(v)), "rock", v)
            self.assertEqual(roads.surface_material_kind(spec(v)), "rock", v)
        for v in ("grass", "grass_paver"):
            self.assertEqual(roads.terrain_surface(spec(v)), "grass", v)
            self.assertEqual(roads.surface_material_kind(spec(v)), "grass", v)
        # everything else is drawn as a road decal, with its own mask material
        self.assertIsNone(roads.terrain_surface(spec("asphalt")))
        self.assertEqual(roads.surface_material_kind(spec("asphalt")), "dirt")
        self.assertIsNone(roads.terrain_surface(spec("sett:pattern")))

    def test_concrete_streets_get_gridmap_v2_concrete(self):
        f = features([[(0, 0), (10, 0)]] * 2, ["residential", "secondary"],
                     surfaces=[(0, "concrete"), (1, "asphalt")])
        s = roads.road_specs(f)
        self.assertEqual(s[0].surf, "concrete")
        self.assertEqual(s[0].kind, "paved", "marks, widths and the tyres' feel stay a paved road's")
        self.assertEqual(roads.look_of(s[0])["m"], "m_gm_concrete_plain")
        self.assertEqual(roads.look_of(s[1])["m"], "road_asphalt_2lane")
        self.assertEqual(roads.look_of(s[0])["tl"], roads.LOOK["paved"]["tl"], "the texture length is asphalt's")

    def test_specs_width_and_left_out(self):
        f = features([[(0, 0), (10, 0)]] * 6, ["residential", "footway", "primary", "tertiary", "track", "secondary"],
                     surfaces=[(4, "gravel")], widths=[(2, 14.0)], levels=[(3, 1.0)], flags=[(5, "is_bridge")])
        s = roads.road_specs(f)
        self.assertEqual(s[0][:2], (roads.STYLE["residential"][0] + 2 * roads.EDGE_STRIP_M, "paved"), "rural: edge strips")
        self.assertIsNone(s[1], "footways left out")
        self.assertEqual(s[2][:2], (14.0, "paved"), "width rule")
        self.assertIsNone(s[3], "off ground level (bridge / tunnel) left out")
        self.assertEqual(s[4][:2], (roads.STYLE["track"][0], "gravel"))
        self.assertIsNone(s[5], "bridge flag left out")

    def test_carriageway_parking_and_strips(self):
        res = roads.STYLE["residential"][0]
        self.assertEqual(roads.carriageway("residential", "paved", True), res + 2 * roads.PARKING_M, "city street: parking")
        self.assertEqual(roads.carriageway("residential", "paved", False), res + 2 * roads.EDGE_STRIP_M, "rural: strips")
        self.assertEqual(roads.carriageway("motorway", "paved", True), roads.STYLE["motorway"][0], "no parking on motorways")
        self.assertEqual(roads.carriageway("service", "paved", False), roads.STYLE["service"][0], "service: as is")
        self.assertEqual(roads.carriageway("track", "dirt", True), roads.STYLE["track"][0], "dirt: as is")
        self.assertEqual(roads.carriageway("residential", "paved", True, 7.5), 7.5, "a width rule is the carriageway")

    def test_urban_from_land_cover(self):
        f = features([[(0, 0), (10, 0)], [(0, 100), (10, 100)]], ["residential", "residential"], widths=[(1, 9.0)])
        s = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))
        self.assertEqual(s[0][0], roads.STYLE["residential"][0] + 2 * roads.PARKING_M)
        self.assertEqual(s[1][0], 9.0, "width rule wins over the parking guess")


class DualCarriagewayTest(unittest.TestCase):
    def feats(self, lines, names, classes=None):
        f = features(lines, classes or ["primary"] * len(lines))
        f.attrs["name"] = np.array(names)
        return f

    def test_divided_road_one_way_each_with_the_other_on_its_left(self):
        # Boston St (Baltimore): two "primary" carriageways 20 m apart, neither one-way in the data
        f = self.feats([[(0, 0), (300, 0)], [(300, 20), (0, 20)], [(0, -80), (300, -80)]],
                       ["Boston Street", "Boston Street", "Boston Street"])
        sp = roads.road_specs(f)
        self.assertTrue(sp[0].oneway and sp[1].oneway, "both carriageways one-way")
        self.assertEqual((sp[0].dir, sp[1].dir), (1, 1), "each runs with the other on its left (driving on the right)")
        self.assertFalse(sp[2].oneway, "a same-named road 80 m off is not a carriageway of it")
        self.assertLess(sp[0].width, roads.carriageway("primary", "paved", False, None, False), "a one-way width")
        lh = roads.road_specs(f, left_hand=True)
        self.assertEqual((lh[0].dir, lh[1].dir), (-1, -1), "driving on the left: the other way")
        ex = roads.extras(sp[0], np.array([[0, 0, 0], [300, 0, 0.0]]), LON)
        self.assertFalse(any(abs(abs(float(pts[0][1])) - roads.CENTRE["gap"]) < 1e-6 for _, _, pts in ex),
                         "no double centre line (a lane line instead)")

    def test_carriageways_never_overlap(self):
        """Two one-way trunk carriageways 9.6 m apart (Munich's McGraw-Graben): each narrower than the gap, not the
        class's 11 m; far apart they keep the class's width."""
        near = self.feats([[(0, 0), (300, 0)], [(300, 9.6), (0, 9.6)]], ["Graben", "Graben"], ["trunk", "trunk"])
        far = self.feats([[(0, 0), (300, 0)], [(300, 30), (0, 30)]], ["Graben", "Graben"], ["trunk", "trunk"])
        sp = roads.road_specs(near)
        for s_ in sp:
            self.assertAlmostEqual(s_.width, 9.6 - roads.DUAL_MIN_MEDIAN_M, delta=0.15)
        w = roads.carriageway("trunk", "paved", False, None, True)
        self.assertTrue(all(abs(s_.width - w) < 1e-9 for s_ in roads.road_specs(far)))

    def test_other_names_and_crossing_roads_untouched(self):
        f = self.feats([[(0, 0), (300, 0)], [(300, 20), (0, 20)], [(150, -100), (150, 100)]],
                       ["Boston Street", "Elliott Street", "Boston Street"])
        self.assertFalse(any(s.oneway for s in roads.road_specs(f)))


class GeometryTest(unittest.TestCase):
    def test_clip(self):
        pts = np.array([[-10.0, 5.0], [20.0, 5.0], [20.0, 30.0], [5.0, 30.0], [5.0, 5.0]])
        pieces = roads.clip_polyline(pts, 0, 0, 10, 10)
        self.assertEqual(len(pieces), 2)
        self.assertTrue(np.allclose(pieces[0], [[0, 5], [10, 5]]))
        self.assertTrue(np.allclose(pieces[1], [[5, 10], [5, 5]]))
        self.assertEqual(roads.clip_polyline(np.array([[20.0, 20], [30, 30]]), 0, 0, 10, 10), [])

    def test_simplify_densify_split(self):
        pts = np.array([[0.0, 0, 0], [10, 0.1, 0], [20, 0, 0], [30, 10, 0]])
        s = roads.simplify(pts)
        self.assertEqual(len(s), 3, "the 0.1 m bump goes, the corner stays")
        d = roads.densify(np.array([[0.0, 0, 0], [100, 0, 0]]))
        self.assertTrue((np.diff(d[:, 0]) <= roads.MAX_SPACING + 1e-9).all() and d[-1, 0] == 100)
        chunks = roads.split(np.arange(250 * 3, dtype=float).reshape(250, 3))
        self.assertTrue(all(len(c) <= roads.MAX_NODES for c in chunks))
        self.assertTrue(all(np.array_equal(a[-1], b[0]) for a, b in zip(chunks[:-1], chunks[1:])), "chunks share their ends")
        self.assertEqual(sum(len(c) for c in chunks) - (len(chunks) - 1), 250)

    def test_a_straight_deck_keeps_its_rise(self):
        # Lombard Street, Baltimore 2026-10-20: on xy alone a straight bridge kept its two ends in a tile (both at
        # 22.2 m) and lost the 2.5 m rise between; the abutment's ground stood over the deck and a car drove into it
        x = np.arange(0.0, 160.0, 10.0)
        z = 22.0 + 2.5 * np.sin(np.pi * x / 160.0)
        pts = np.c_[x, np.zeros_like(x), z]
        self.assertEqual(len(roads.simplify(pts)), 2, "xy alone: the ends")
        kept = roads.simplify(pts, ztol=roads.SIMPLIFY_Z_M)
        dense = roads.densify(kept)
        self.assertLess(np.abs(np.interp(x, dense[:, 0], dense[:, 2]) - z).max(), roads.SIMPLIFY_Z_M + 1e-9)
        spec = roads.road_specs(features([[(0, 0), (160, 0)]], ["residential"]))[0]
        chunks = roads._deck_chunks([("b", spec, pts, LON)], (40.0, -50.0, 170.0, 50.0))   # (cut by a tile edge)
        deck = np.concatenate([np.asarray(c["n"]) for c in chunks if c.get("deck") and not c.get("ctx")])
        self.assertLess(np.abs(np.interp(deck[:, 0], x, z) - deck[:, 2]).max(), 0.1, "the deck's rise drawn")

    def test_tiles_near(self):
        ks = roads.tiles_near(0, 0, 300)
        self.assertIn((0, 0), ks)
        self.assertIn((-2, -2), ks)
        self.assertNotIn((2, 0), ks)
        self.assertEqual(roads.tile_distance(0, 0, 10, 10), 0.0)
        self.assertEqual(roads.tile_distance(1, 0, 10, 10), roads.TILE - 10)


class BuildTileTest(unittest.TestCase):
    """The road tiles with their carriageway meshes (BNGE_ROADS_IN_GROUND=0; InGroundTest: the default)."""

    def setUp(self):
        self._rig, roads.ROADS_IN_GROUND = roads.ROADS_IN_GROUND, False

    def tearDown(self):
        roads.ROADS_IN_GROUND = self._rig

    def test_build_clips_and_styles(self):
        f = features([[(-300, 50), (300, 50)], [(100, 10), (100, 200)]], ["primary", "track"])
        chunks = [c for c in roads.build_tile(FRAME, 0, 0, [f], flat) if c["m"] in {l["m"] for l in roads.LOOK.values()}]
        self.assertEqual(len(chunks), 2, "one surface chunk per road (lines and sidewalks aside)")
        prim = next(c for c in chunks if c["w"] == roads.STYLE["primary"][0] + 2 * roads.EDGE_STRIP_M)
        xs = [n[0] for n in prim["n"]]
        self.assertAlmostEqual(min(xs), -roads.OVERLAP, delta=0.05)
        self.assertAlmostEqual(max(xs), roads.TILE + roads.OVERLAP, delta=0.05)
        self.assertTrue(all(abs(n[1] - 50) < 0.3 for n in prim["n"]), "on its line (the test helper is spherical: ~0.13 m)")
        self.assertEqual(prim["m"], roads.LOOK["paved"]["m"])
        track = next(c for c in chunks if c is not prim)
        self.assertEqual(track["m"], roads.LOOK["dirt"]["m"])
        self.assertTrue(all(abs(n[2] - 10.0) < 0.05 for n in prim["n"]), "z on the DEM (flat, near the anchor)")

    def test_ramps_one_lane_wide(self):
        f = features([[(0, 0), (100, 0)]] * 3, ["motorway", "motorway", "primary"])
        f.rules["subclass"] = rules([(0, "link"), (2, "link")])
        f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)], "float")
        s = roads.road_specs(f)
        self.assertEqual(s[0].width, roads.LINK_M[1], "a one-way motorway ramp")
        self.assertGreater(s[1].width, 10.0, "the motorway itself")
        self.assertEqual(s[2].width, roads.LINK_M[2], "a two-way slip road")
        self.assertEqual(roads.bridge_specs(features([[(0, 0), (100, 0)]], ["motorway"])), [None])

    def test_motorway_lanes_rural_two_urban_three(self):
        """GitHub #43: every US Interstate had three lanes a direction, rural ones mostly have two; a rural road's edge
        strips were counted as lanes (a rural divided trunk had three too)."""
        pts = np.array([(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)])

        def lanes(cls, urban):
            sp = roads.Spec(roads.carriageway(cls, "paved", urban, None, True), "paved", urban, cls, True, 2.5, 1)
            n = sum(1 for look, w, line in roads.extras(sp, pts, -100.0) if look is roads.LANE_LINE)
            return n + 1, sp
        n, rural = lanes("motorway", False)
        self.assertEqual(n, 2, "rural motorway carriageway")
        self.assertAlmostEqual(rural.width, roads.MOTORWAY_RURAL_M + 2 * roads.SHOULDER_M)
        edges = [line[0][1] for look, w, line in roads.extras(rural, pts, -100.0) if look is roads.EDGE_LINE]
        self.assertEqual(sorted(round(abs(y), 2) for y in edges), [round(roads.MOTORWAY_RURAL_M / 2, 2)] * 2,
                         "edge lines between the lanes and the shoulders")
        self.assertEqual(lanes("motorway", True)[0], 3, "urban motorway carriageway")
        self.assertEqual(lanes("trunk", False)[0], 2, "rural divided trunk")
        # a ramp (one lane, LINK_M): its edge lines a lane apart, not inside the motorway's shoulders (GitHub #107)
        ramp = roads.Spec(roads.LINK_M[1], "paved", False, "motorway", True, 2.5, 1)
        edges = [line[0][1] for look, w, line in roads.extras(ramp, pts, -100.0) if look is roads.EDGE_LINE]
        self.assertAlmostEqual(abs(edges[0] - edges[1]), roads.LINK_LANE_M, places=2)

    def test_divided_trunk_sidewalk_outer_side_only_when_slow(self):
        pts = np.array([(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)])

        def walks(spec, left_hand=False):
            return [line[0][1] for look, w, line in roads.extras(spec, pts, -75.0, left_hand)
                    if look["m"] == roads.SIDEWALK["m"]]
        slow = roads.Spec(7.0, "paved", True, "trunk", True, 2.5, 1, 56.0)
        self.assertEqual(len(walks(slow)), 1)
        self.assertLess(walks(slow)[0], 0, "driving east on the right: the curb is south, the median north")
        self.assertGreater(walks(slow._replace(dir=-1))[0], 0)
        self.assertGreater(walks(slow, left_hand=True)[0], 0)
        self.assertEqual(walks(slow._replace(kmh=100.0)), [], "an expressway carriageway: none")
        self.assertEqual(walks(slow._replace(kmh=None)), [], "unknown limit and junctions: none")
        self.assertEqual(len(walks(slow._replace(kmh=None, gap=50.0))), 1, "no limit, a junction every block")
        self.assertEqual(walks(slow._replace(kmh=None, gap=900.0)), [], "no limit, interchanges far apart")
        f = features([[(0, 0), (300, 0)]], ["trunk"])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 0.5, 0.5), (0, "C", 1.0, 1.0)])
        self.assertAlmostEqual(roads.road_specs(f)[0].gap, 150.0, delta=1.0)
        self.assertEqual(len(walks(roads.Spec(9.0, "paved", True, "trunk", False))), 2, "a two-way main street")

    def test_city_street_sidewalks_and_lines_stop_at_crossings(self):
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool))
        by = {}
        for c in chunks:
            by.setdefault(c["m"], []).append(c)
        self.assertIn(roads.SIDEWALK["m"], by, "city streets get sidewalks")
        self.assertIn(roads.GUTTER["m"], by)
        self.assertIn("line_yellow", by, "double yellow centre line (Americas)")
        self.assertNotIn(roads.EDGE_LINE["m"], by, "no rural edge lines in the city")
        sec_w = roads.carriageway("secondary", "paved", True)
        res_w = roads.carriageway("residential", "paved", True)
        for c in by["line_yellow"]:              # the secondary's centre lines stop at the residential street
            for x, y, _ in c["n"]:
                self.assertFalse(abs(x - 120) < res_w / 2, (x, y))
        for c in by[roads.SIDEWALK["m"]]:        # no sidewalk crosses another street's carriageway
            for x, y, _ in c["n"]:
                on_sec = abs(y - 100) < sec_w / 2
                on_res = abs(x - 120) < res_w / 2
                self.assertFalse(on_sec and on_res, (x, y))
        sw = roads.SIDEWALK["w"]               # the corner: the secondary's sidewalk runs on to the
        sec_walk = [c for c in by[roads.SIDEWALK["m"]] if abs(c["n"][0][1] - 100) > 1 and   # residential's curb
                    all(abs(n[1] - c["n"][0][1]) < 0.01 for n in c["n"])]
        res_walk = [c for c in by[roads.SIDEWALK["m"]] if c not in sec_walk]
        xs = sorted(x for c in sec_walk for x, _, _ in (c["n"][0], c["n"][-1]))
        self.assertTrue(any(abs(x - (120 - res_w / 2)) < 0.6 for x in xs), ("to the curb", xs))
        for c in res_walk:                       # ... and the residential's stops at its outer edge
            for x, y, _ in c["n"]:
                self.assertGreater(abs(y - 100), sec_w / 2 + sw - 0.6, (x, y))
        yc = next(c["n"][0][1] for c in by[roads.LOOK["paved"]["m"]] if abs(c["n"][0][1] - 100) < 1)
        ys = sorted({round(c["n"][0][1], 3) for c in by["line_yellow"]})
        self.assertTrue(any(abs(y - (yc + roads.CENTRE["gap"])) < 0.01 for y in ys) and
                        any(abs(y - (yc - roads.CENTRE["gap"])) < 0.01 for y in ys), (yc, ys))

    def test_sidewalk_mesh_runs_and_drains(self):
        import tempfile
        import cdae
        import sidewalks
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        self.assertFalse(any(c["m"] == roads.SIDEWALK["m"] for c in chunks), "sidewalks are the mesh, not decals")
        self.assertTrue(walks and {w["side"] for w in walks} == {1, -1})
        x0, y0, x1, y1 = roads.tile_bbox(0, 0)
        for w in walks:                              # clipped exactly to the tile; caps only at real ends
            self.assertTrue((w["pts"][:, 0] >= x0 - 1e-6).all() and (w["pts"][:, 0] <= x1 + 1e-6).all())
            for end, cap in ((w["pts"][0], w["caps"][0]), (w["pts"][-1], w["caps"][1])):
                self.assertEqual(cap, not roads._on_box(end, (x0, y0, x1, y1)))
        dr = [c for c in chunks if c["m"] == roads.DRAIN["m"]]
        self.assertTrue(dr, "storm drains at the corners")
        sec_w = roads.carriageway("secondary", "paved", True)
        curbs = [sidewalks.run_geometry(w["pts"], w["side"], roads.SIDEWALK["w"]) for w in walks]
        for c in dr:              # in the gutter: GUTTER w / 2 from a curb, on the road's side of it
            p = np.mean(np.array(c["n"])[:, :2], axis=0)
            best = min(((roads._seg_dist(p[None], cu[:-1, :2], cu[1:, :2]).min(), cu, o) for cu, o in curbs),
                       key=lambda t: t[0])
            d, cu, o = best
            k = int(np.argmin(np.hypot(*(cu[:, :2] - p).T)))
            self.assertAlmostEqual(d, roads.GUTTER["w"] / 2, delta=0.05)
            self.assertLess(float((p - cu[k, :2]) @ o[k]), 0, "on the road side of the curb")
        with tempfile.TemporaryDirectory() as d:
            path, n = roads.write_tile(d, "/vfs", 1, 0, 0, chunks, walks)
            import json
            with open(os.path.join(d, os.path.basename(path))) as fh:
                data = json.load(fh)
            self.assertEqual(data["walk"]["pos"], [x0, y0, 0.0])
            shape = cdae.read(os.path.join(d, os.path.basename(data["walk"]["shape"])))
            names = [m["name"] for m in shape["materials"]]      # the ones it uses, in the list's order (cdae.encode)
        self.assertEqual(names, [m for m in sidewalks.MATERIALS if m in names])
        self.assertIn("curbs", names)

    def test_sidewalks_follow_the_ground(self):
        # a straight street (two vertices) across a 5 m dip: the road mesh drapes on the ground every 2 m, the
        # sidewalks ran as a chord between the vertices, up to 5 m in the air beside it
        import sidewalks

        def dip(lat, lon):
            e = (np.asarray(lon) - LON) * M_LON
            return 10.0 - 5.0 * np.exp(-((e - 128.0) / 30.0) ** 2)

        def ground(xy):
            la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            return FRAME.geodetic_to_enu(la, lo, dip(la, lo))[2]
        f = features([[(-300, 100), (300, 100)]], ["secondary"])
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], dip, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        # both sides (1.5.5 leaves a sidewalk out where the ground falls away from the road, so a side can be in
        # pieces); every piece is checked below
        self.assertEqual({w["side"] for w in walks}, {1, -1})
        for w in walks:
            curb, _ = sidewalks.run_geometry(w["pts"], w["side"], w["w"])
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(curb[:, :2], axis=0).T))]
            fine = np.arange(0.0, s[-1], 0.5)               # between the run's points too (the mesh is linear there)
            xy = np.stack([np.interp(fine, s, curb[:, c]) for c in range(2)], axis=1)
            dz = np.interp(fine, s, curb[:, 2]) - ground(xy)
            self.assertLess(np.abs(dz).max(), 0.05, "the curb on the ground under it")
        for c in chunks:                                    # storm drains in the gutter, on the ground too
            if c["m"] == roads.DRAIN["m"]:
                n = np.array(c["n"])
                self.assertLess(np.abs(n[:, 2] - ground(n[:, :2])).max(), 0.05)

    def test_sidewalks_on_the_drawn_ground(self):
        # the road surfaces lie on the terrain block's own heights (its 2 m lattice rounded to the quantum, each
        # square cut south-west to north-east: roadmesh.follow), elev's smooth curve off them by up to the quantum and
        # more between the samples; the curbs take the same ground (the user, 2026-10-21)
        import roadmesh
        import sidewalks

        def hill(lat, lon):
            e = (np.asarray(lon) - LON) * M_LON
            n = (np.asarray(lat) - LAT) * M_LAT
            return 10.0 + 0.04 * e - 3.0 * np.exp(-((e - 128.0) ** 2 + (n - 147.0) ** 2) / 8.0 ** 2)   # a dip on the street

        def ground(x, y):
            return roadmesh.drawn_ground(FRAME, hill, np.c_[x, y], 1 / 32)
        f = features([[(-300, 40), (300, 190)]], ["secondary"])
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], hill, lambda la, lo: np.ones(np.shape(la), bool), walks=walks,
                                  ground=ground)
        # both sides (1.5.5 leaves a sidewalk out where the ground falls away from the road, so a side can be in
        # pieces); every piece is checked below
        self.assertEqual({w["side"] for w in walks}, {1, -1})
        for w in walks:
            curb, _ = sidewalks.run_geometry(w["pts"], w["side"], w["w"])
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(curb[:, :2], axis=0).T))]
            fine = np.arange(0.0, s[-1], 0.5)
            xy = np.stack([np.interp(fine, s, curb[:, c]) for c in range(2)], axis=1)
            dz = np.interp(fine, s, curb[:, 2]) - ground(xy[:, 0], xy[:, 1])
            self.assertLess(np.abs(dz).max(), 0.025, "the curb on the drawn ground under it")
        for c in chunks:
            if c["m"] == roads.DRAIN["m"]:
                n = np.array(c["n"])
                self.assertLess(np.abs(n[:, 2] - ground(n[:, 0], n[:, 1])).max(), 0.025)

    def test_sidewalks_on_flat_ground_keep_their_points(self):
        # the drape adds points only where the ground bends: a flat street's runs are as before (MAX_SPACING apart)
        f = features([[(-300, 100), (300, 100)]], ["secondary"])
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        for w in walks:
            p = w["pts"]
            length = float(np.hypot(*np.diff(p[:, :2], axis=0).T).sum())
            self.assertEqual(len(p), math.ceil(length / roads.MAX_SPACING - 1e-9) + 1)

    def test_sidewalk_profile_faces_out(self):
        import sidewalks
        run = np.array([[0.0, 5.0, 10.0], [20.0, 5.0, 10.0], [40.0, 8.0, 10.0]])
        for side in (1, -1):
            curb, out = sidewalks.run_geometry(run, side, 2.5)
            b = sidewalks.Builder((0.0, 0.0, 0.0))
            b.add_run(curb, out, 2.5)
            P, T, UV, M = b.arrays()
            n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
            n /= np.linalg.norm(n, axis=1)[:, None]
            top = n[:, 2] > 0.9
            self.assertTrue(top.any())
            self.assertAlmostEqual(P[:, 2].max(), 10.0 + sidewalks.CURB_H + 2.5 * sidewalks.CROSS_SLOPE, places=6)
            # the curb face looks at the road (away from the sidewalk)
            face = (np.abs(n[:, 2]) < 0.3) & (M == 0)
            c = P[T[face]].mean(1)
            near_curb = np.abs(np.hypot(*(c[:, :2] - curb[1, :2]).T)) < 15
            o3 = np.r_[out[1], 0.0]
            looks = n[face][near_curb] @ o3
            self.assertTrue((looks < 0).any(), "a face toward the road")
            # the back slopes down to the ground (1:2, sidewalks.BACK_RUN): it looks outward and up, a wheel rolls on it
            back = (M == 0) & (n[:, 2] > 0.5) & (n[:, 2] < 0.95) & ((n[:, :2] @ out[1]) > 0.3)
            self.assertTrue(back.any(), "a sloped back face")

    def test_junction_corners_crosswalks_ramps(self):
        import tempfile
        import junctions
        import sidewalks
        # four residential streets meeting at (120, 120) (Overture: split at the junction)
        X = (120.0, 120.0)
        arms = [[X, (300, 120)], [X, (120, 300)], [X, (-60, 120)], [X, (120, -60)]]
        f = features(arms, ["residential"] * 4)
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        corners = [w["corner"] for w in walks if "corner" in w]
        runs = [w for w in walks if "corner" not in w]
        self.assertEqual(len(corners), 4, "a curb return in each quadrant")
        cw = [c for c in chunks if c["m"] == junctions.CROSSWALK["m"]]
        self.assertEqual(len(cw), 4, "a crosswalk on each arm")
        self.assertTrue(sum(len(w["ramps"]) for w in runs) + sum(len(c["ramps"]) for c in corners) >= 8,
                        "two curb ramps per crosswalk")
        # residential streets with parking: curb extensions (bulb-outs) at the corners
        wd = roads.carriageway("residential", "paved", True) / 2 - roads.PARKING_M
        cls_ = [np.array(c["n"])[:, :2] for c in chunks if c["m"] == roads.LOOK["paved"]["m"] and c["n"]]
        for c in corners:
            d = np.min([junctions._seg_dist(c["arc"][:, :2], ln) for ln in cls_], axis=0)
            self.assertGreater(float(d.min()), wd - 0.1, "the curb never on the driving lanes")
            self.assertLess(float(d.min()), wd + 0.3, "the bulb reaches the driving lanes")
            self.assertEqual(len(c["ramps"]), 2, "the crosswalks land on the bulbs")
        for c in chunks:
            if c["m"] == junctions.CROSSWALK["m"]:
                self.assertAlmostEqual(np.hypot(*np.subtract(c["n"][0][:2], c["n"][-1][:2])), 2 * wd, delta=0.3)
        w = roads.carriageway("residential", "paved", True) / 2
        for c in chunks:                        # no markings through the junction or its crosswalks
            if c["m"].startswith("line_") and c["w"] != junctions.STOP_LINE["w"]:
                for x, y, _ in c["n"]:
                    self.assertGreater(max(abs(x - X[0]), abs(y - X[1])), w + junctions.RADII["residential"], (x, y))
        stops = [c for c in chunks if c["m"] == junctions.STOP_LINE["m"] and c["w"] == junctions.STOP_LINE["w"]]
        self.assertEqual(len(stops), 4, "a stop line on each approach")
        for c in stops:                         # on the approach half, beyond the crosswalk
            a, b = np.array(c["n"][0][:2]), np.array(c["n"][-1][:2])
            self.assertLess(np.hypot(*(b - a)), w)
        fills = [c for c in chunks if c.get("poly")]
        self.assertEqual(len(fills), 4, "the corner asphalt")
        for c in corners:                       # the arc keeps RADIUS from its centre, between the curbs
            arc = c["arc"]
            d = np.hypot(arc[:, 0] - X[0], arc[:, 1] - X[1])
            self.assertTrue((d > w - 0.5).all(), d.min())
        near = [r for r in runs if np.hypot(*(r["pts"][0, :2] - X)) < 30 or np.hypot(*(r["pts"][-1, :2] - X)) < 30]
        for r in near:                          # sidewalk ends at a corner: no end cap there
            for end, cap in ((r["pts"][0], r["caps"][0]), (r["pts"][-1], r["caps"][1])):
                if np.hypot(*(end[:2] - X)) < 30:
                    self.assertFalse(cap)
        with tempfile.TemporaryDirectory() as d:
            mesh = __import__("roadmesh").build(FRAME, chunks, flat, (0.0, 0.0, 0.0))
            path, n = roads.write_tile(d, "/vfs", 1, 0, 0, chunks, walks, mesh)
            import cdae
            shape = cdae.read(os.path.join(d, "walks_1_0_0.cdae"))
            names = [m["name"] for m in shape["materials"]]      # the ones it uses, in the list's order (cdae.encode)
        self.assertEqual(names, [m for m in sidewalks.MATERIALS if m in names])
        self.assertIn("curbs", names)

    def test_acute_corner_is_rounded(self):
        # two streets leaving a junction 30 degrees apart (a triangular block's tip, Fells Point, report #46): a curb
        # return round the tip, not the sidewalks meeting in a wedge
        import junctions
        X = (120.0, 120.0)
        c30 = (120 + 180 * math.cos(math.radians(30)), 120 + 180 * math.sin(math.radians(30)))
        arms = [[X, (300, 120)], [X, c30], [X, (-60, 120)]]
        f = features(arms, ["residential"] * 3)
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        corners = [w["corner"] for w in walks if "corner" in w]
        tips = [c for c in corners if np.mean(c["arc"][:, 1]) > 120 and np.mean(c["arc"][:, 0]) > 125
                and np.mean(c["arc"][:, 1]) < 120 + (np.mean(c["arc"][:, 0]) - 120) * 0.6]
        self.assertEqual(len(tips), 1, "the tip has its curb return")
        arc = tips[0]["arc"][:, :2]
        self.assertLess(float(np.hypot(*(arc - np.array(X)).T).min()), 40.0)
        self.assertLessEqual(junctions.ACUTE_R_MIN, junctions.RADII["residential"])

    def test_junction_short_connector_no_corner_on_roads(self):
        import junctions
        # two parallel streets joined by an 8 m connector (two junctions 8 m apart): no curb return
        # may put slab on a carriageway; crosswalks never cross another road
        arms = [[(-200, 100), (0, 100)], [(0, 100), (200, 100)], [(-200, 108), (0, 108)], [(0, 108), (200, 108)],
                [(0, 100), (0, 108)]]
        f = features(arms, ["residential"] * 5)
        walks = []
        chunks = roads.build_tile(FRAME, -1, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        w = roads.carriageway("residential", "paved", True) / 2
        lines = [np.array([(-200, 100), (200, 100)]), np.array([(-200, 108), (200, 108)]), np.array([(0, 100), (0, 108)])]
        for c in [wk["corner"] for wk in walks if "corner" in wk]:
            probe = c["arc"][:, :2] + c["inward"] * 1.0
            for ln in lines:
                self.assertGreater(junctions._seg_dist(probe, ln).min(), w - 0.5)
        for c in chunks:
            if c["m"] == junctions.CROSSWALK["m"]:
                a, b = np.array(c["n"][0][:2]), np.array(c["n"][-1][:2])
                for ln in lines:
                    if junctions._seg_dist(np.array([(a + b) / 2]), ln)[0] > 1.0:
                        self.assertFalse(junctions._crosses(a, b, ln))

    def test_offset_junction_corners_across_the_connector(self):
        import junctions
        # two junctions ~10 m apart on a diagonal connector no longer than the streets are wide (the user's
        # Fells Point junction, 2026-10-17): every quadrant gets a curb return, turning onto the connector's edge
        # at one node and off at the other, none on a carriageway, and the connector keeps no sidewalk
        A, B = (120.0, 120.0), (127.0, 113.0)
        arms = [[A, (120, 300)], [A, (-60, 120)], [A, B], [B, (127, -60)], [B, (300, 113)]]
        f = features(arms, ["residential"] * 5)
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        corners = [wk["corner"] for wk in walks if "corner" in wk]
        self.assertEqual(len(corners), 4)
        w = roads.carriageway("residential", "paved", True) / 2
        lines = [np.array(a, np.float64) for a in arms]
        for c in corners:
            probe = c["arc"][:, :2] + c["inward"] * 1.0
            for ln in lines:
                self.assertGreater(junctions._seg_dist(probe, ln).min(), w - 0.5 - roads.PARKING_M)
        mid = np.array([(A[0] + B[0]) / 2, (A[1] + B[1]) / 2])
        for r in [wk for wk in walks if "pts" in wk]:
            self.assertGreater(np.hypot(*(r["pts"][:, :2] - mid).T).min(), w, "no sidewalk along the connector")

    def test_cobblestone_strips_no_paint(self):
        self.assertEqual(roads.surface_kind("residential", "paving_stones"), "cobble")
        f = features([[(-300, 50), (300, 50)]], ["tertiary"], surfaces=[(0, "paving_stones")])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool))
        cob = [c for c in chunks if c["m"] == roads.LOOK["cobble"]["m"]]
        w = roads.carriageway("tertiary", "cobble", True)
        n = max(1, int(round(w / roads.LOOK["cobble"]["strip"])))
        self.assertEqual(len(cob), n, "parallel strips across the width")
        self.assertTrue(all(abs(c["w"] - w / n) < 0.01 for c in cob))
        self.assertTrue(any(c["m"] == roads.SIDEWALK["m"] for c in chunks), "cobbled city streets keep sidewalks")
        self.assertFalse(any(c["m"].startswith("line_") for c in chunks), "no lines painted on cobbles")

    def test_tower_passages_stay_bridge(self):
        """Tower Bridge: level 1 over 0.03-0.88, flags bridge / tunnel (through a tower, ~10 m) / bridge / tunnel /
        bridge: one bridge (the touching ranges merged, tunnel winning, made it a tunnel left out under water)."""
        fl = [(0, "is_bridge", 0.03, 0.34), (0, "is_tunnel", 0.34, 0.37), (0, "is_bridge", 0.37, 0.56),
              (0, "is_tunnel", 0.56, 0.59), (0, "is_bridge", 0.59, 0.88)]
        f = features([[(-190, 0), (190, 0)]], ["trunk"], levels=[(0, 1.0, 0.03, 0.88)], flags=fl)
        (a, b, k), = roads.level_ranges(f, 0)
        self.assertEqual(k, "bridge")
        self.assertAlmostEqual(a, 0.03, places=5)
        self.assertAlmostEqual(b, 0.88, places=5)
        # a real tunnel next to a bridge stays a tunnel; a level range where no flag is keeps its kind
        f2 = features([[(-500, 0), (500, 0)]], ["trunk"], levels=[(0, -1.0, 0.0, 0.2)],
                      flags=[(0, "is_bridge", 0.2, 0.3), (0, "is_tunnel", 0.3, 0.8)])
        self.assertEqual([k for _, _, k in roads.level_ranges(f2, 0)], ["tunnel", "bridge", "tunnel"])

    def test_partial_bridge_kept_and_decked(self):
        # 400 m street; level 1 on 40-60 % of it (a bridge in the middle): ground parts drawn, the
        # bridge part a deck between the two abutments' heights (the ground slopes 5 % east)
        f = features([[(-200, 50), (200, 50)]], ["secondary"], levels=[(0, 1.0, 0.4, 0.6)])
        (a, b, k), = roads.level_ranges(f, 0)
        self.assertAlmostEqual(a, 0.4, places=5)
        self.assertAlmostEqual(b, 0.6, places=5)
        self.assertEqual(k, "bridge")
        self.assertEqual(len(roads.ground_parts(f, 0)), 2)
        self.assertEqual(len(roads.bridge_parts(f, 0)), 1)
        self.assertIsNotNone(roads.road_specs(f)[0], "a street with a bridge in it stays")
        self.assertIsNotNone(roads.bridge_specs(f)[0])

        def slope(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 10.0 + 0.05 * e
        chunks = roads.build_tile(FRAME, -1, 0, [f], slope)
        decks = [c for c in chunks if c.get("deck")]
        self.assertTrue(decks)
        for c in decks:
            for x, y, z in c["n"]:
                self.assertTrue(-41 <= x <= 1, x)              # the bridge range, clipped to the tile
                self.assertAlmostEqual(z, 10.0 + 0.05 * x, delta=0.3)   # straight between abutments
        asphalt = [c for c in chunks if c["m"] == roads.LOOK["paved"]["m"] and not c.get("deck")]
        for c in asphalt:
            for x, _, _ in c["n"]:
                self.assertFalse(-39 < x < -1, x)               # no ground road over the bridge range
        tunnel = features([[(-200, 50), (200, 50)]], ["secondary"], flags=[(0, "is_tunnel", 0.0, 1.0)])
        self.assertIsNone(roads.road_specs(tunnel)[0])
        self.assertIsNone(roads.bridge_specs(tunnel)[0])

    def test_offset_and_colour(self):
        pts = np.array([[0.0, 0, 5], [10, 0, 5], [10, 10, 5]])
        o = roads.offset_polyline(pts, 1.0)
        self.assertTrue(np.allclose(o[0], [0, 1, 5]) and np.allclose(o[-1], [9, 10, 5]))
        self.assertTrue(np.allclose(o[1], [9, 1, 5]), "mitred corner")
        self.assertEqual(roads.centre_colour(-76.6), "line_yellow")
        self.assertEqual(roads.centre_colour(12.5), "line_white")
        # the Americas' double yellow line; elsewhere one broken white line (the Gotthard road, 2026-10-18)
        sp = roads.Spec(7.0, "paved", False, "primary", False)
        line = np.array([[0.0, 0, 5], [100, 0, 5]])
        us = [lk for lk, w, ln in roads.extras(sp, line, -76.6) if lk["m"] == "line_yellow"]
        eu = [lk for lk, w, ln in roads.extras(sp, line, 8.6) if lk["m"] in ("line_white", roads.LANE_LINE["m"])
              and lk is not roads.EDGE_LINE]
        self.assertEqual(len(us), 2)
        self.assertEqual([lk["m"] for lk in eu], [roads.LANE_LINE["m"]])

    def test_feature_in_two_cells_once_and_deterministic(self):
        f = features([[(-300, 50), (300, 50)]], ["primary"])
        a = roads.build_tile(FRAME, 0, 0, [f, f], flat)
        b = roads.build_tile(FRAME, 0, 0, [f], flat)
        self.assertEqual(a, b)

    def test_write_tile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, n = roads.write_tile(tmp, "/temp/beamngEarth/s", 3, -1, 2, [{"m": "x", "w": 1, "tl": 1, "p": 1, "n": [[0, 0, 0], [1, 0, 0]]}])
            self.assertEqual((path, n), ("/temp/beamngEarth/s/roads_3_-1_2.json", 1))
            with open(os.path.join(tmp, "roads_3_-1_2.json")) as fh:
                self.assertEqual(json.load(fh)["chunks"][0]["m"], "x")


    def test_collider_boxes_deck(self):
        deck = {"deck": True, "w": 8, "n": [[0, 0, 10], [50, 0, 12]]}
        boxes = roads.collider_boxes(None, [deck, {"w": 6, "n": [[0, 0, 0], [5, 0, 0]]}])
        self.assertEqual(list(boxes), ["deck"])
        b = np.array(boxes["deck"])                    # every piece with the point before it: the span covered
        self.assertTrue(b[:, 0].min() < 0 and b[:, 3].max() > 50 and b[:, 1].min() <= -4.5 and b[:, 4].max() >= 4.5)
        self.assertTrue(b[:, 2].min() < 9 and b[:, 5].max() >= 14)
        self.assertEqual(roads.collider_boxes(None, None), {})

class FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class TrackerTest(unittest.TestCase):
    def run_poses(self, poses):
        conn = FakeConn()
        tr = roads.Tracker()
        built = []

        def build(aid, tx, ty):
            built.append((aid, tx, ty))
            return (f"/p/{aid}_{tx}_{ty}", 1)

        async def go():
            loop = asyncio.get_running_loop()
            for aid, x, y in poses:
                for _ in range(40):             # several poses at one place: every tile gets built
                    tr.on_pose(conn, aid, x, y, loop, build)
                    await asyncio.sleep(0.01)
        asyncio.run(go())
        return conn, tr, built

    def test_sends_near_tiles_once(self):
        conn, tr, built = self.run_poses([(1, 0.0, 0.0)])
        keys = {(m["anchorId"], m["key"]) for m in conn.sent if m["type"] == "roads_tile"}
        want = {(1, f"{tx}_{ty}") for tx, ty in roads.tiles_near(0, 0, roads.SEND_M)}
        self.assertEqual(keys, want)
        self.assertEqual(len(built), len(want), "each tile built once")

    def test_empty_and_failed_tiles_not_built_at_every_pose(self):
        # (from PR #64) a tile with no roads came back None and was built again at every pose; a failed one too, at
        # once: an empty one now stays done while within reach, a failed one waits TILE_RETRY_S
        conn = FakeConn()
        tr = roads.Tracker()
        built = []
        fail = {(0, 0)}

        def build(aid, tx, ty):
            built.append((tx, ty))
            if (tx, ty) in fail:
                raise OSError("offline")
            return None if (tx + ty) % 2 else (f"/p/{aid}_{tx}_{ty}", 1)

        async def go():
            loop = asyncio.get_running_loop()
            for _ in range(40):
                tr.on_pose(conn, 1, 0.0, 0.0, loop, build)
                await asyncio.sleep(0.01)
        asyncio.run(go())
        near = set(roads.tiles_near(0, 0, roads.SEND_M))
        counts = {k: built.count(k) for k in near}
        self.assertTrue(all(counts[k] == 1 for k in near if (k[0] + k[1]) % 2), "empty tiles built once")
        self.assertEqual(counts[(0, 0)], 1, "the failed one not again within TILE_RETRY_S")
        tr.retry[(0, 0)] = 0.0              # its wait over
        fail.clear()
        asyncio.run(go())
        self.assertEqual(built.count((0, 0)), 2)
        self.assertIn((1, "0_0"), {(m["anchorId"], m["key"]) for m in conn.sent if m["type"] == "roads_tile"})

    def test_a_tile_failing_again_and_again_is_logged(self):
        # (GitHub #91) network errors are expected and retried quietly, but a tile that never comes left no trace
        conn = FakeConn()
        tr = roads.Tracker()

        def build(aid, tx, ty):
            if (tx, ty) == (0, 0):
                raise OSError("Access is denied")
            return None

        async def go():
            loop = asyncio.get_running_loop()
            for _ in range(10):
                tr.on_pose(conn, 1, 0.0, 0.0, loop, build)
                await asyncio.sleep(0.01)
        with self.assertLogs("earthd.roads", "WARNING") as cm:
            for _ in range(roads.TILE_FAILS_WARN):
                tr.retry.clear()
                asyncio.run(go())
        self.assertEqual(len(cm.output), 1)
        self.assertIn("Access is denied", cm.output[0])

    def test_far_buildings_skip_the_road_tiles(self):
        import vegetation
        import buildings
        conn = FakeConn()
        tr = vegetation.Tracker(buildings.FAR_SEND_M, buildings.FAR_DROP_M, buildings.FAR_PER_POSE, "bldfar",
                                skip_m=roads.SEND_M)

        async def go():
            loop = asyncio.get_running_loop()
            for _ in range(200):
                tr.on_pose(conn, 1, 0.0, 0.0, loop, lambda aid, tx, ty: (f"/p/{tx}_{ty}", 1))
                await asyncio.sleep(0.005)
        asyncio.run(go())
        keys = {tuple(map(int, m["key"].split("_"))) for m in conn.sent if m["type"] == "bldfar_tile"}
        near = set(roads.tiles_near(0, 0, roads.SEND_M))
        self.assertEqual(keys, set(roads.tiles_near(0, 0, buildings.FAR_SEND_M)) - near)
        self.assertTrue(near and not (keys & near))

    def test_at_speed_the_tiles_reach_along_the_path_nearest_first(self):
        # 100 m/s east: the corridor ahead up to LEAD_S of driving (LEAD_MAX_M), only a strip of it beside the path
        want = roads.wanted_tiles(0.0, 0.0, 100.0, 0.0)
        near = set(roads.tiles_near(0, 0, roads.SEND_M))
        ahead = want - near
        self.assertTrue(ahead, "tiles beyond SEND_M along the path")
        self.assertTrue(all(roads.tile_bbox(*k)[0] > 0 for k in ahead), "only ahead (east)")
        far_x = max(roads.tile_bbox(*k)[0] for k in ahead)
        lead = min(roads.LEAD_MAX_M, 100.0 * roads.LEAD_S)
        self.assertGreaterEqual(far_x + roads.TILE, lead - roads.TILE / 2, "up to LEAD_S of driving")
        self.assertTrue(all(abs((roads.tile_bbox(*k)[1] + roads.tile_bbox(*k)[3]) / 2) <= roads.CORRIDOR_M + roads.TILE
                            for k in ahead), "a strip along the path")
        self.assertEqual(roads.wanted_tiles(0.0, 0.0, 10.0, 0.0), near, "slow: the square around the car only")
        # built in the order the car reaches them: the car's tile, the path ahead, then beside, behind last
        key = roads.path_key(128.0, 128.0, 100.0, 0.0)
        order = sorted([(-2, 0), (0, 2), (5, 0), (1, 0), (0, 0)], key=key)
        self.assertEqual(order[:3], [(0, 0), (1, 0), (5, 0)])
        self.assertEqual(order[-1], (-2, 0))

    def test_at_speed_the_path_ahead_is_built_and_kept(self):
        conn = FakeConn()
        tr = roads.Tracker()
        built = []

        def build(aid, tx, ty):
            built.append((tx, ty))
            return (f"/p/{aid}_{tx}_{ty}", 1)

        async def go():
            loop = asyncio.get_running_loop()
            for _ in range(80):
                tr.on_pose(conn, 1, 128.0, 128.0, loop, build, vel=(100.0, 0.0))
                await asyncio.sleep(0.01)
        asyncio.run(go())
        want = roads.wanted_tiles(128.0, 128.0, 100.0, 0.0)
        self.assertEqual(set(built), want, "every wanted tile built once")
        self.assertFalse([m for m in conn.sent if m["type"] == "roads_drop"], "the path ahead is not dropped")
        first = built[:6]
        self.assertEqual(first[0], (0, 0), "the car's own tile first")
        self.assertTrue(all(k[0] >= 0 and abs(k[1]) <= 1 for k in first), f"then the path ahead: {first}")

    def test_ground_first_no_path_tiles_while_a_block_builds(self):
        conn = FakeConn()
        tr = roads.Tracker()
        built = []

        async def go():
            loop = asyncio.get_running_loop()
            for _ in range(60):
                tr.on_pose(conn, 1, 128.0, 128.0, loop, lambda aid, tx, ty: (built.append((tx, ty)), (f"/p/{tx}_{ty}", 1))[1],
                           vel=(100.0, 0.0), ground_busy=True)
                await asyncio.sleep(0.01)
        asyncio.run(go())
        self.assertEqual(set(built), set(roads.tiles_near(128.0, 128.0, roads.SEND_M)), "only the square around the car")

    def test_an_away_camera_gets_its_tiles_after_the_cars_and_keeps_them(self):
        # the user, 2026-10-20: what is drawn follows the camera, what collides the car
        import vegetation
        for make, kind in ((roads.Tracker, "roads"), (vegetation.Tracker, "veg")):
            conn, built = FakeConn(), []
            tr = make()
            cam = (5000.0, 0.0)

            async def go(view, n=60):
                loop = asyncio.get_running_loop()
                for _ in range(n):
                    tr.on_pose(conn, 1, 0.0, 0.0, loop, lambda aid, tx, ty: (built.append((tx, ty)), (f"/p/{tx}_{ty}", 1))[1],
                               view=view)
                    await asyncio.sleep(0.01)
            send = roads.SEND_M if kind == "roads" else vegetation.SEND_M
            car, seen = set(roads.tiles_near(0, 0, send)), set(roads.tiles_near(*cam, send))
            asyncio.run(go(cam))
            self.assertEqual(set(built), car | seen, kind)
            self.assertEqual(set(built[:len(car)]), car, f"{kind}: the car's tiles first")
            self.assertFalse([m for m in conn.sent if m["type"].endswith("_drop")], f"{kind}: none dropped")
            asyncio.run(go(None, 2))            # the camera back at the car: the camera's tiles go
            drops = {tuple(map(int, m["key"].split("_"))) for m in conn.sent if m["type"].endswith("_drop")}
            self.assertEqual(drops, seen, kind)

    def test_other_vehicles_foci_get_their_tiles_after_the_cars_and_keep_them(self):
        # BeamMP support (the user, 2026-10-22): collision islands round other players' cars (the mod's
        # collisionfoci.lua); road tiles for every focus, tree tiles for the ones whose trees collide
        import vegetation
        for make, kind in ((roads.Tracker, "roads"), (vegetation.Tracker, "veg")):
            conn, built = FakeConn(), []
            tr = make()
            focus = (4000.0, 0.0, 320.0, True)

            async def go(foci, n=60):
                loop = asyncio.get_running_loop()
                for _ in range(n):
                    tr.on_pose(conn, 1, 0.0, 0.0, loop, lambda aid, tx, ty: (built.append((tx, ty)), (f"/p/{tx}_{ty}", 1))[1],
                               foci=foci)
                    await asyncio.sleep(0.01)
            send = roads.SEND_M if kind == "roads" else vegetation.SEND_M
            car = set(roads.tiles_near(0, 0, send))
            near = set(roads.tiles_near(4000.0, 0.0, min(send, 320.0 + roads.TILE / 2)))
            asyncio.run(go((focus,)))
            self.assertEqual(set(built), car | near, kind)
            self.assertEqual(set(built[:len(car)]), car, f"{kind}: the car's tiles first")
            self.assertFalse([m for m in conn.sent if m["type"].endswith("_drop")], f"{kind}: none dropped")
            asyncio.run(go((), 2))              # the other car gone: its tiles go
            drops = {tuple(map(int, m["key"].split("_"))) for m in conn.sent if m["type"].endswith("_drop")}
            self.assertEqual(drops, near, kind)

    def test_parse_foci(self):
        import earthd
        f = earthd.parse_foci([{"x": 10, "y": -20, "r": 320, "trees": True}, {"x": 1, "y": 2},
                               {"x": float("nan"), "y": 0, "r": 80}, {"x": 1e9, "y": 0, "r": 80},
                               {"x": 5, "y": 5, "r": 99999}, "junk"])
        self.assertEqual(f, ((10.0, -20.0, 320.0, True), (5.0, 5.0, earthd.FOCI_R_MAX_M, False)))
        self.assertEqual(earthd.parse_foci(None), ())
        self.assertEqual(len(earthd.parse_foci([{"x": 0, "y": 0, "r": 80}] * 40)), earthd.FOCI_MAX)

    def test_drops_far_and_resends_on_anchor_change(self):
        conn, tr, built = self.run_poses([(1, 0.0, 0.0), (1, 3000.0, 0.0), (2, 3000.0, 0.0)])
        drops = [m for m in conn.sent if m["type"] == "roads_drop"]
        self.assertTrue(drops and all(m["anchorId"] == 1 for m in drops))
        self.assertTrue(all(roads.tile_distance(*map(int, m["key"].split("_")), 3000.0, 0.0) > roads.DROP_M for m in drops))
        a2 = {m["key"] for m in conn.sent if m["type"] == "roads_tile" and m["anchorId"] == 2}
        self.assertEqual(a2, {f"{tx}_{ty}" for tx, ty in roads.tiles_near(3000.0, 0.0, roads.SEND_M)})


class PaintRoadsTest(unittest.TestCase):
    def grid(self, n=128):
        rows = np.arange(n)[:, None]
        cols = np.arange(n)[None, :]
        lat = LAT + (n / 2 - rows) * 2 / M_LAT + np.zeros((1, n))
        lon = LON + (cols - n / 2) * 2 / M_LON + np.zeros((n, 1))
        return lat, lon

    def test_affine(self):
        lat, lon = self.grid()
        f = sf.grid_affine(lat, lon)
        c, r = f(lon[10, 20], lat[10, 20])
        self.assertAlmostEqual(float(c), 20, places=3)
        self.assertAlmostEqual(float(r), 10, places=3)

    def test_paved_and_dirt_painted_at_width(self):
        lat, lon = self.grid()
        f = features([[(-200, 0), (200, 0)], [(20, -200), (20, 200)]], ["residential", "track"])
        idx = np.full(lat.shape, sf.GRASS, np.uint8)
        out = sf.paint_roads(idx, lat, lon, [f], 2.0)
        self.assertEqual(int(out[64, 10]), sf.ASPHALT, "on the east-west street")
        self.assertEqual(int(out[64 - 6, 10]), sf.GRASS, "12 m north of it: off the 8 m street (6 m + strips)")
        self.assertEqual(int(out[30, 74]), sf.DIRT, "on the track (20 m east)")
        self.assertEqual(int(out[64, 74]), sf.ASPHALT, "paved wins at the crossing")
        self.assertEqual(int(idx[64, 10]), sf.GRASS, "input untouched")

    def test_bare_verge_beside_roads(self):
        """Grass tufts stood on the asphalt along road edges (the user, 2026-10-20): a road without sidewalks gets a
        bare verge (VERGE_M a side) where the ground carries grass; rock stays rock."""
        lat, lon = self.grid()
        f = features([[(-200, 0), (200, 0)]], ["residential"])
        out = sf.paint_roads(np.full(lat.shape, sf.GRASS, np.uint8), lat, lon, [f], 2.0)
        col = [int(v) for v in out[:, 10]]
        self.assertEqual(col[64 - 2], sf.ASPHALT, "4 m north: the 8 m road's edge, painted")
        self.assertEqual(col[64 + 2], sf.ASPHALT, "4 m south: likewise (centred, not half a sample off)")
        self.assertEqual((col[64 - 3], col[64 + 3]), (sf.VERGE, sf.VERGE), "6 m either side: the verge")
        self.assertEqual(col[64 - 6], sf.GRASS, "12 m north: grass again")
        rock = sf.paint_roads(np.full(lat.shape, sf.ROCK, np.uint8), lat, lon, [f], 2.0)
        self.assertEqual(int(rock[64 - 3, 10]), sf.ROCK)
        f9 = features([[(-200, 0), (200, 0)]], ["residential"], widths=[(0, 9.0)])
        out9 = sf.paint_roads(np.full(lat.shape, sf.GRASS, np.uint8), lat, lon, [f9], 2.0)
        n = int((out9[:, 10] == sf.ASPHALT).sum())
        self.assertGreaterEqual(n * 2.0, 9.0, "a 9 m road painted at least 9 m wide (was 8)")

    def test_cobbles_under_the_tyres(self):
        lat, lon = self.grid()
        f = features([[(-200, 0), (200, 0)]], ["residential"], surfaces=[(0, "paving_stones")])
        out = sf.paint_roads(np.full(lat.shape, sf.GRASS, np.uint8), lat, lon, [f], 2.0)
        self.assertEqual(int(out[64, 10]), sf.COBBLE)
        self.assertEqual(sf.MATERIALS[sf.COBBLE][1], "COBBLESTONE")
        self.assertEqual(sf.DIRT, len(sf.MATERIALS) - 1, "dirt still last")

    def test_a_tunnels_open_section_painted(self):
        # a 'tunnel' in the map lying open (grading.Grader.open_tunnels): road, not grass (Central Park, 2026-10-17)
        lat, lon = self.grid()
        f = features([[(-200, 0), (200, 0)]], ["residential"], flags=[(0, "is_tunnel")])
        idx = np.full(lat.shape, sf.GRASS, np.uint8)
        self.assertEqual(int(sf.paint_roads(idx, lat, lon, [f], 2.0)[64, 10]), sf.GRASS, "a tunnel: not painted")
        la = np.array([LAT, LAT])
        lo = LON + np.array([-200.0, 200.0]) / M_LON
        out = sf.paint_roads(idx, lat, lon, [f], 2.0, extra=[(la, lo, roads.Spec(7.0, "paved", False, "residential"))])
        self.assertEqual(int(out[64, 10]), sf.ASPHALT)


class InGroundTest(unittest.TestCase):
    """Carriageways painted into the ground (roads.ROADS_IN_GROUND, the default): no carriageway meshes, the markings
    kept."""

    def setUp(self):
        self._rig, roads.ROADS_IN_GROUND = roads.ROADS_IN_GROUND, True

    def tearDown(self):
        roads.ROADS_IN_GROUND = self._rig

    def test_no_carriageway_mesh_markings_kept(self):
        f = features([[(-300, 50), (300, 50)]], ["primary"])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
        surfaces = {l["m"] for l in roads.LOOK.values()}
        self.assertFalse([c for c in chunks if c["m"] in surfaces], "no carriageway mesh")
        self.assertTrue([c for c in chunks if c["m"] not in surfaces], "its lines stay")


if __name__ == "__main__":
    unittest.main()


class MedianTest(unittest.TestCase):
    def test_divided_road_has_a_median(self):
        # report #48 (Boston Street): the carriageways' inner sides a curbed band and lawn, not two sidewalks
        sp = roads.Spec(7.0, "paved", True, "secondary", True, 2.5, 1, None, None, 19.0)
        self.assertTrue(roads.has_median(sp))
        self.assertEqual(roads.walk_sides(sp), (-1,), "the sidewalk on the outer (right) side only")
        self.assertEqual(roads.median_side(sp), 1)
        pts = np.c_[np.arange(0.0, 50.1, 5.0), np.zeros(11), np.zeros(11)]
        walks = [(lk, w) for lk, w, _ in roads.extras(sp, pts, -76.6) if lk["m"] == roads.SIDEWALK["m"]]
        self.assertEqual(sorted(w for _, w in walks), [roads.MEDIAN_CURB_W, sp.walk], "a wide median: curb bands")
        narrow = sp._replace(median=11.0)           # Boston Street: 4.5 m of median, one island curb to curb
        walks = [w for lk, w, _ in roads.extras(narrow, pts, -76.6) if lk["m"] == roads.SIDEWALK["m"]]
        self.assertIn(1.85, walks)
        # two one-way streets a block apart: not a median
        far = sp._replace(median=60.0)
        self.assertFalse(roads.has_median(far))
        self.assertEqual(roads.walk_sides(far), (1, -1))

    def test_median_painted_lawn(self):
        import surface as sf
        n = 128
        rows = np.arange(n)[:, None]
        cols = np.arange(n)[None, :]
        lat = LAT + (n / 2 - rows) * 2 / M_LAT + np.zeros((1, n))
        lon = LON + (cols - n / 2) * 2 / M_LON + np.zeros((n, 1))
        f = features([[(-200, 0), (200, 0)], [(200, 22), (-200, 22)]], ["secondary", "secondary"])
        f.attrs["name"] = np.array(["Boston Street", "Boston Street"], object)
        out = sf.paint_roads(np.full(lat.shape, sf.GRASS, np.uint8), lat, lon, [f], 2.0)
        self.assertEqual(int(out[64 - 5, 64]), sf.LAWN, "between the carriageways (a wide median): lawn")
        self.assertEqual(int(out[64, 64]), sf.ASPHALT)


class WallRunsTest(unittest.TestCase):
    def test_no_wall_under_a_bridge_deck(self):
        # the Bruce Highway (the user's report, 2026-10-21): beside a motorway under an overpass the ground is the
        # abutment's fill; the wall rose to it, through the deck
        xy = np.c_[np.zeros(21), np.arange(-20.0, 21.0, 2.0)]          # a wall line along a road at z 8
        z = np.full(len(xy), 8.0)
        deck = {"deck": True, "w": 10.0, "n": [[-50.0, 0.0, 16.0], [50.0, 0.0, 16.0]]}   # 8 m over it, 10 m wide
        under = roads._under_decks(xy, z, [deck])
        self.assertTrue(under[np.abs(xy[:, 1]) <= 5 + roads.WALL_DECK_MARGIN_M].all())
        self.assertFalse(under[np.abs(xy[:, 1]) > 5 + roads.WALL_DECK_MARGIN_M + 0.5].any())
        low = dict(deck, n=[[-50.0, 0.0, 9.0], [50.0, 0.0, 9.0]])      # a deck at the road's own level: its approach
        self.assertFalse(roads._under_decks(xy, z, [low]).any())

    def test_one_wall_over_a_wavering_cut(self):
        cut = np.array([0, 0.5, 2.5, 1.9, 2.2, 1.5, 0.9, 1.0, 2.4, 2.6, 1.3, 0.2, 0, 2.1, 0, 0, 0])
        w = roads._wall_runs(cut)
        self.assertTrue(w[2:11].all(), "one wall from 2 to 10 (the 0.9 / 1.0 dip filled)")
        self.assertFalse(w[:2].any() or w[11:].any(), "no stub for a lone 2.1 m sample")


class ViewOrderTest(unittest.TestCase):
    def test_tiles_in_view_come_first(self):
        # the camera looks east: a tile 600 m east is built before one 400 m west, the car's own tile first
        import types
        import roads as r
        conn = types.SimpleNamespace(cam={"x": 0.0, "y": 0.0, "fx": 1.0, "fy": 0.0, "fovDeg": 60}, cam_aid=1)
        key = r.view_key(conn, 1, 128.0, 128.0)
        want = [(-2, 0), (3, 0), (0, 0)]
        self.assertEqual(sorted(want, key=key), [(0, 0), (3, 0), (-2, 0)])
        plain = r.view_key(types.SimpleNamespace(cam=None), 1, 128.0, 128.0)
        self.assertEqual(sorted(want, key=plain), [(0, 0), (-2, 0), (3, 0)])
        other = r.view_key(conn, 2, 128.0, 128.0)            # (the camera of another anchor's frame: ignored)
        self.assertEqual(sorted(want, key=other), [(0, 0), (-2, 0), (3, 0)])


class ChainResolveTest(unittest.TestCase):
    """BridgeProfiles._resolve: a cell loaded at a loose end can put a longer copy of a part already in the chain into
    the pool (its ends then new); the chain is followed on instead of failing (KeyError, a Chicago road tile,
    2026-10-24), whatever order the set of keys comes in."""

    def test_part_grown_by_a_loaded_cell(self):
        spec = type("Spec", (), {"width": 8.0})()

        def part(x0, x1):
            xy = np.array([[x0, 0.0], [x1, 0.0]])
            return (spec, xy, LAT, 1.0, 2)

        class Grown(roads.BridgeProfiles):
            def _load(self, key, pool, ground):
                if pool[("b", 0)][1][-1][0] < 300:
                    pool[("b", 0)] = (spec, np.array([[100.0, 0.0], [300.0, 0.0]]), LAT, 1.0, 9)

        for first in (("a", 0), ("b", 0)):
            res = Grown(FRAME, lambda key: None, lambda la, lo: np.zeros(np.shape(la)))
            pool = {("a", 0): part(0.0, 100.0), ("b", 0): part(100.0, 200.0)}
            if first == ("b", 0):
                pool = {("b", 0): pool[("b", 0)], ("a", 0): pool[("a", 0)]}
            self.assertEqual(res._resolve(("a", 0), pool, {}, set()), [("a", 0), ("b", 0)])
            self.assertEqual(float(pool[("b", 0)][1][-1][0]), 300.0)
