"""Park alleys, footpaths, cycleways and their furniture (walkways.py)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import walkways  # noqa: E402

LAT0, LON0 = 50.5354, 16.2330           # Police nad Metuji, Bezdekuv sad
FRAME = geodesy.EnuFrame(LAT0, LON0, 0.0)
BOX = (-100.0, -100.0, 100.0, 100.0)


def flat(la, lo):
    return np.full(np.shape(la), 100.0)


def ll(xy):
    xy = np.asarray(xy, float)
    la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.column_stack((lo, la))


def layer(layer_name, rows, **cols):
    """Features of rows [(kind, xy list, id)] with attribute columns (lists, one value per row)."""
    geoms, ids = [], []
    for kind, xy, fid in sorted(rows, key=lambda r: r[2]):
        geoms.append((kind, [[ll(xy)]]))
        ids.append(fid)
    f = mapgeom.build(layer_name, np.array(ids), geoms)
    for k, v in cols.items():
        f.attrs[k] = mapgeom.float_column(v) if k == "osm_width" else mapgeom.text_column(v)
    f.own = np.ones(len(ids), bool)
    return f


def square(cx, cy, w, h):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2),
            (cx - w / 2, cy - h / 2)]


L, P, G = mapgeom.LINE, mapgeom.POINT, mapgeom.POLYGON


def scene(extra_paths=(), park=(0, 0, 60, 50), pcols=None):
    """A park with a gravel cross, a road south of it, a sidewalk beside the road and a crossing."""
    rows = [(L, [(-30, 0), (30, 0)], "a-we"), (L, [(0, -25), (0, 25)], "b-ns"),
            (L, [(-80, -60), (80, -60)], "c-road"), (L, [(-80, -57), (80, -57)], "d-sidewalk"),
            (L, [(-70, -62), (-70, -52)], "e-cross"), (L, [(70, 40), (90, 40)], "f-walk"),
            (L, [(60, 70), (90, 70)], "g-trail"), (L, [(-90, 80), (90, 80)], "h-cycle")] + list(extra_paths)
    rows.sort(key=lambda r: r[2])
    n = len(rows)
    cls = {"a-we": "footway", "b-ns": "footway", "c-road": "residential", "d-sidewalk": "footway", "e-cross": "footway",
           "f-walk": "footway", "g-trail": "path", "h-cycle": "cycleway"}
    sub = {"d-sidewalk": "sidewalk", "e-cross": "crosswalk"}
    surf = {"a-we": "gravel", "b-ns": "gravel"}
    cols = dict(**{"class": [cls.get(r[2], "footway") for r in rows], "subclass": [sub.get(r[2], "") for r in rows],
                   "osm_surface": [surf.get(r[2], "") for r in rows], "osm_lit": [""] * n,
                   "osm_sidewalk": ["separate" if r[2] == "c-road" else "" for r in rows], "name": [""] * n})
    for k, v in (pcols or {}).items():
        cols[k] = [v.get(r[2], cols[k][i]) for i, r in enumerate(rows)]
    roads = layer("roads", rows, **cols)
    land = layer("land_use", [(G, square(*park), "p1")], **{"class": ["park"], "name": ["Sad"]})
    infra = layer("infrastructure", [(P, [(10, 8)], "i1"), (P, [(-80, -61)], "i2")],
                  **{"class": ["bench", "street_lamp"]})
    return walkways.read(FRAME, [roads], [land], [infra], BOX)


class ReadTest(unittest.TestCase):
    def test_paths_drives_parks_points(self):
        d = scene()
        self.assertEqual(sorted(p.fid.split(".")[0] for p in d.paths),
                         ["a-we", "b-ns", "d-sidewalk", "e-cross", "f-walk", "g-trail", "h-cycle"])
        self.assertEqual(len(d.drives), 1)
        self.assertEqual(d.drives[0][2], "separate")
        self.assertEqual(len(d.parks), 1)
        self.assertAlmostEqual(d.parks[0][1], 3000.0, delta=30.0)
        self.assertEqual(sorted(c for c, _x, _y in d.points), ["bench", "street_lamp"])
        a = [p for p in d.paths if p.fid.startswith("a-we")][0]
        self.assertEqual(a.surface, "gravel")
        self.assertAlmostEqual(float(np.hypot(*np.diff(a.xy, axis=0).T).sum()), 60.0, delta=0.5)

    def test_private_garden_is_no_park(self):
        land = layer("land_use", [(G, square(0, 0, 30, 30), "g1")], **{"class": ["garden"], "name": [""]})
        self.assertEqual(walkways.read(FRAME, [], [land], [], BOX).parks, [])


class LongCrosswalkTest(unittest.TestCase):
    """Overture's subclass crosswalk on a way of 100 m or more is a boulevard's alley (Vinnytsia: the south half of the Solar
    System alley, 639 m): read as a plain footway; a real crossing stays one."""

    def test_long_crosswalk_is_a_footway(self):
        long_cross = ("z-long", [(-85, 100), (85, 100)])
        d = scene(extra_paths=[(L, long_cross[1], "z-long")],
                  pcols={"subclass": {"z-long": "crosswalk"}, "osm_surface": {"z-long": "paving_stones"}})
        by = {p.fid.split(".")[0]: p for p in d.paths}
        self.assertEqual(by["z-long"].sub, "")                 # 170 m: a footway
        self.assertEqual(by["e-cross"].sub, "crosswalk")       # 10 m: still a crossing
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            roles = {w.fid.split(".")[0]: w for w in walkways.classify(d)}
        self.assertIn("z-long", roles)
        self.assertNotIn("e-cross", roles)
        self.assertEqual(roles["z-long"].kind, "stone")


class ClassifyTest(unittest.TestCase):
    def roles(self, **kw):
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            return {w.fid.split(".")[0]: w for w in walkways.classify(scene(**kw))}

    def test_roles(self):
        r = self.roles()
        self.assertEqual(r["a-we"].role, "park")             # gravel, unnamed, unlit: a plain park path
        self.assertEqual(r["f-walk"].role, "walk")           # a footway outside a park
        self.assertEqual(r["g-trail"].role, "trail")
        self.assertEqual(r["h-cycle"].role, "cycleway")
        self.assertNotIn("e-cross", r)                       # a crossing has its markings
        self.assertEqual(r["d-sidewalk"].role, "sidewalk")   # its road says sidewalk=separate: nothing else draws it

    def test_sidewalk_the_road_draws_is_dropped(self):
        r = self.roles(pcols={"osm_sidewalk": {"c-road": "both"}})
        self.assertNotIn("d-sidewalk", r)
        r = self.roles(pcols={"osm_sidewalk": {"c-road": ""}})      # no tag: the land cover rule may leave it bare
        self.assertEqual(r["d-sidewalk"].role, "sidewalk")

    def test_main_paths_of_a_park(self):
        for cols in ({"osm_lit": {"a-we": "yes"}}, {"osm_width": {"a-we": 3.5}}, {"name": {"a-we": "Alej"}},
                     {"osm_surface": {"a-we": "asphalt"}}):
            if "osm_width" in cols:
                with mock.patch.object(walkways, "_photo", return_value=(None, False)):
                    d = scene()
                    d.paths[0].width = 3.5
                    r = {w.fid.split(".")[0]: w for w in walkways.classify(d)}
            else:
                r = self.roles(pcols=cols)
            self.assertEqual(r["a-we"].role, "alley", cols)
            self.assertEqual(r["b-ns"].role, "park", cols)

    def test_width_is_the_mapped_one_else_the_roles(self):
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            d = scene()
            d.paths[0].width = 12.0
            d.paths[1].width = 0.1
            r = {w.fid.split(".")[0]: w for w in walkways.classify(d)}
        self.assertEqual(r["a-we"].width, walkways.WIDTH_MAX_M)
        self.assertEqual(r["b-ns"].width, walkways.WIDTH_MIN_M)
        self.assertEqual(r["h-cycle"].width, 2.0)                # a cycleway without a width: 2 m (UA 1.8)

    def test_surface_tag_then_photo_then_role(self):
        r = self.roles()
        self.assertEqual((r["a-we"].kind, r["a-we"].src), ("gravel", "tag"))
        self.assertEqual((r["f-walk"].kind, r["f-walk"].src), ("gravel", "near"))     # the park's gravel cross nearby
        with mock.patch.object(walkways, "_near_surface", return_value=None),                 mock.patch.object(walkways, "_photo", return_value=(None, False)):    # (no photo tiles on this disk)
            r = {w.fid.split(".")[0]: w for w in walkways.classify(scene())}
        self.assertEqual((r["f-walk"].kind, r["f-walk"].src), ("asphalt", "default"))
        with mock.patch.object(walkways, "_photo", return_value=("gravel", False)), \
                mock.patch.object(walkways, "_near_surface", return_value=None):
            w = {w.fid.split(".")[0]: w for w in walkways.classify(scene())}
        self.assertEqual((w["f-walk"].kind, w["f-walk"].src), ("gravel", "photo"))
        self.assertEqual(w["a-we"].src, "tag")               # the tag is first
        with mock.patch.object(walkways, "_photo", return_value=(None, True)):
            w = walkways.classify(scene())
        self.assertTrue(walkways.any_photo_missing(w))

    def test_drawn(self):
        r = self.roles()
        self.assertTrue(r["a-we"].drawn)
        self.assertFalse(r["g-trail"].drawn)                 # a bare trail stays the photo's
        r2 = self.roles(pcols={"osm_surface": {"g-trail": "asphalt"}})
        self.assertTrue(r2["g-trail"].drawn)


class StripTest(unittest.TestCase):
    def test_area_and_winding(self):
        P, T, s = walkways.strip(np.array([[0.0, 0.0], [40.0, 0.0]]), 3.0)
        a, b, c = (P[T[:, k]] for k in range(3))
        cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
        self.assertTrue((cross > 0).all())                   # counter-clockwise from above
        self.assertAlmostEqual(float(cross.sum() / 2), 120.0, places=6)
        self.assertLessEqual(float(np.diff(s[::2]).max()), walkways.STEP_M + 1e-9)

    def test_closed_ring_has_one_seam_normal(self):
        t = np.linspace(0, 2 * np.pi, 40)
        ring = np.column_stack((8 * np.cos(t), 8 * np.sin(t)))
        P, T, _s = walkways.strip(ring, 2.0)
        self.assertTrue(np.allclose(P[0], P[-2], atol=1e-6) and np.allclose(P[1], P[-1], atol=1e-6))

    def test_degenerate_line_gives_nothing(self):
        self.assertIsNone(walkways.strip(np.array([[1.0, 1.0], [1.0, 1.0]]), 2.0))


class MeshTest(unittest.TestCase):
    def build(self, box=BOX, drives=(), **kw):
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            d = scene(**kw)
            walks = walkways.classify(d)
        return walkways.mesh(FRAME, box, walks, flat, (0.0, 0.0, 0.0), None, drives or d.drives), walks

    def test_strips_follow_the_ground_and_use_one_material_per_kind(self):
        m, walks = self.build()
        V, T, UV, mats, idx = m
        self.assertIn("bne_dirt_gravel", mats)               # the park's gravel cross
        self.assertIn("bne_eu_asphalt_walk", mats)           # the footway and the cycleway
        self.assertTrue(np.all((V[:, 2] > 100.0 + walkways.LIFT_M - 1e-3) & (V[:, 2] < 100.1)))
        self.assertEqual(len(idx), len(T))
        self.assertTrue(0 <= T.min() and T.max() < len(V) and len(UV) == len(V))

    def test_a_tile_takes_only_its_own_triangles(self):
        a, _ = self.build(box=(-100.0, -100.0, 0.0, 100.0))
        b, _ = self.build(box=(0.0, -100.0, 100.0, 100.0))
        whole, _ = self.build()
        self.assertEqual(len(a[1]) + len(b[1]), len(whole[1]))
        self.assertLessEqual(float(a[0][:, 0].max()), 3.0)
        self.assertGreaterEqual(float(b[0][:, 0].min()), -3.0)

    def test_nothing_on_a_drive_road(self):
        d = scene()
        road = [dr for dr in d.drives]
        m, _ = self.build(extra_paths=[(L, [(-40, -61), (-40, -50)], "i-over")])
        self.assertIsNotNone(m)
        P, T = m[0], m[1]
        c = P[T].mean(1)
        on = (np.abs(c[:, 1] + 60.0) < road[0][1] - 0.2) & (np.abs(c[:, 0]) < 78)
        self.assertFalse(on.any())

    def test_no_drawn_walk_no_mesh(self):
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            walks = [w for w in walkways.classify(scene()) if not w.drawn]
        self.assertIsNone(walkways.mesh(FRAME, BOX, walks, flat, (0, 0, 0), None, ()))

    def test_harder_kinds_lie_higher(self):
        self.assertLess(walkways.RANK["gravel"], walkways.RANK["asphalt"])


class CorridorTest(unittest.TestCase):
    def test_widths_and_exclusions(self):
        roads = scene().paths                                # (the layer itself is rebuilt for the corridor)
        rows = [(L, [(0, 0), (30, 0)], "a-park"), (L, [(0, 5), (30, 5)], "b-walk"), (L, [(0, 9), (30, 9)], "c-side"),
                (L, [(0, 12), (30, 12)], "d-wide")]
        f = layer("roads", rows, **{"class": ["path", "footway", "footway", "cycleway"],
                                    "subclass": ["", "", "sidewalk", ""], "osm_width": [None, None, None, 4.0]})
        idx, hw = walkways.corridor(f)
        self.assertEqual(list(idx), [0, 1, 3])               # the sidewalk keeps its street trees
        self.assertAlmostEqual(hw[0], 1.2 / 2 + walkways.CORRIDOR_CLEAR_M)
        self.assertAlmostEqual(hw[1], 2.0 / 2 + walkways.CORRIDOR_CLEAR_M)
        self.assertAlmostEqual(hw[2], 4.0 / 2 + walkways.CORRIDOR_CLEAR_M)
        self.assertTrue(len(roads))

    def test_off_flag(self):
        f = layer("roads", [(L, [(0, 0), (30, 0)], "a")], **{"class": ["path"]})
        with mock.patch.object(walkways, "ON", False):
            self.assertEqual(len(walkways.corridor(f)[0]), 0)


class FurnitureTest(unittest.TestCase):
    def walks(self, park, lit=None, name="", road=True):
        rows = [(L, [(-40, 0), (40, 0)], "a-alley")]
        if road:
            rows.append((L, [(-80, -9), (80, -9)], "z-road"))
        cls = ["footway"] + (["residential"] if road else [])
        n = len(rows)
        roads = layer("roads", rows, **{"class": cls, "subclass": [""] * n, "osm_surface": ["asphalt"] + [""] * (n - 1),
                                        "osm_lit": [lit or ""] + [""] * (n - 1), "name": [name] + [""] * (n - 1)})
        land = layer("land_use", [(G, square(0, 0, *park), "p1")], **{"class": ["park"], "name": [""]})
        infra = layer("infrastructure", [(P, [(0, 1.0), ][0:1], "i1")][:0] or [(P, [(5, 3)], "i1")],
                      **{"class": ["bench"]})
        d = walkways.read(FRAME, [roads], [land], [infra], BOX)
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            return d, walkways.classify(d)

    def test_lit_alley_gets_lamps_along_it_off_the_road(self):
        d, w = self.walks((90, 40), lit="yes")
        self.assertEqual(w[0].role, "alley")
        got = walkways.furniture(d, w, BOX)
        lamps = sorted([g for g in got if g[0] == walkways.LAMP], key=lambda g: g[1])
        self.assertGreaterEqual(len(lamps), 2)
        for a, b in zip(lamps, lamps[1:]):
            self.assertGreaterEqual(float(np.hypot(a[1] - b[1], a[2] - b[2])), walkways.LAMP_SPACING_M * 0.9)
        for g in lamps:
            self.assertAlmostEqual(abs(g[2]), w[0].width / 2 + walkways.LAMP_SIDE_M, delta=0.1)
            self.assertEqual(g[5], walkways.LAMP_HEIGHT[walkways.LAMP])

    def test_unlit_unmapped_park_out_of_town_has_no_lamps_but_benches(self):
        d, w = self.walks((90, 40), name="Alej")
        got = walkways.furniture(d, w, BOX)
        self.assertEqual([g for g in got if g[0] == walkways.LAMP], [])
        self.assertTrue(any(g[0] == walkways.BENCH for g in got))

    def test_town_park_lights_its_alleys(self):
        with mock.patch.object(walkways, "_urban", return_value=True):
            d, w = self.walks((200, 120), name="Alej")       # 2.4 ha in a town, lighting not mapped
        got = walkways.furniture(d, w, BOX)
        self.assertTrue(any(g[0] == walkways.LAMP for g in got))

    def test_lit_no_is_never_lit(self):
        d, w = self.walks((200, 120), lit="no", name="Alej")
        self.assertEqual([g for g in walkways.furniture(d, w, BOX) if g[0] == walkways.LAMP], [])

    def test_mapped_bench_kept_and_faces_the_path(self):
        d, w = self.walks((90, 40), name="Alej")
        got = walkways.furniture(d, w, BOX)
        mapped = [g for g in got if g[0] == walkways.BENCH and abs(g[1] - 5) < 1e-6 and abs(g[2] - 3) < 1e-6]
        self.assertEqual(len(mapped), 1)
        self.assertLess(mapped[0][4], 0)                     # faces south, to the alley at y = 0
        near = [g for g in got if g[0] == walkways.BENCH and abs(g[1] - 5) < 8 and g is not mapped[0]]
        self.assertEqual(near, [])                           # no automatic bench within 8 m of a mapped one

    def test_nothing_outside_the_tile(self):
        d, w = self.walks((90, 40), lit="yes")
        got = walkways.furniture(d, w, (-100.0, -100.0, -60.0, 100.0))
        self.assertTrue(all(g[1] < -60.0 for g in got))

    def test_rows_have_ground_height_and_lamp_lenses(self):
        d, w = self.walks((90, 40), lit="yes")
        rows = walkways.rows_for(FRAME, d, w, BOX, flat)
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(len(r), 8)
        lamps = [r for r in rows if r[0] == walkways.LAMP]
        self.assertTrue(all(abs(r[3] - 100.0) < 1e-6 for r in lamps))
        self.assertGreater(len(rows), len([r for r in rows if r[0] in (walkways.LAMP, walkways.BENCH, walkways.BIN)]))

    def test_deterministic(self):
        d, w = self.walks((90, 40), lit="yes")
        self.assertEqual(walkways.furniture(d, w, BOX), walkways.furniture(d, w, BOX))


class FakeMD:
    """mapdata.MapData's cell(): a small park scene's three layers for any key (fail: a cell that cannot be read)."""

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def cell(self, key, layers=(), kind="demand"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("cell unreadable")
        return _Cell(_LAYERS())


class _Cell:
    def __init__(self, layers):
        self.layers = layers


def _LAYERS():
    rows = [(L, [(-30, 0), (30, 0)], "a-we"), (L, [(0, -25), (0, 25)], "b-ns")]
    roads = layer("roads", rows, **{"class": ["footway", "footway"], "subclass": ["", ""],
                                    "osm_surface": ["gravel", "gravel"], "osm_lit": ["", ""], "name": ["", ""]})
    land = layer("land_use", [(G, square(0, 0, 60, 50), "p1")], **{"class": ["park"], "name": ["Sad"]})
    infra = layer("infrastructure", [(P, [(10, 8)], "i1")], **{"class": ["bench"]})
    return {"roads": roads, "land_use": land, "infrastructure": infra}


class TileHookTest(unittest.TestCase):
    """blockworker._with_walkways and walkways.rows on a map cell, as a road tile build calls them."""

    def setUp(self):
        walkways._MEMO.clear()
        import blockworker
        self.bw = blockworker

    def test_tile_gets_the_strips_merged_into_its_mesh(self):
        m = self.bw._with_walkways(None, FRAME, BOX, flat, (0.0, 0.0, 0.0), FakeMD())
        self.assertIsNotNone(m)
        self.assertIn("bne_dirt_gravel", m[3])
        self.assertEqual(len(m[1]), len(m[4]))

    def test_an_existing_mesh_is_kept_and_extended(self):
        V = np.array([[0.0, 0, 0], [1, 0, 0], [0, 1, 0]])
        base = (V, np.array([[0, 1, 2]]), np.zeros((3, 2)), ["road_asphalt_2lane"], np.array([0]))
        with mock.patch("plazas._covered", return_value=np.zeros(0, bool)):
            m = self.bw._with_walkways(base, FRAME, BOX, flat, (0.0, 0.0, 0.0), FakeMD())
        self.assertEqual(m[3][0], "road_asphalt_2lane")
        self.assertGreater(len(m[1]), 1)
        self.assertTrue(np.array_equal(m[0][:3], V))

    def test_off_no_map_or_failure_leaves_the_mesh_alone(self):
        self.assertIsNone(self.bw._with_walkways(None, FRAME, BOX, flat, (0.0, 0.0, 0.0), None))
        with mock.patch.object(walkways, "ON", False):
            self.assertIsNone(self.bw._with_walkways(None, FRAME, BOX, flat, (0.0, 0.0, 0.0), FakeMD()))
        self.assertIsNone(self.bw._with_walkways(None, FRAME, BOX, flat, (0.0, 0.0, 0.0), FakeMD(fail=True)))
        self.assertEqual(walkways.rows(FRAME, None, BOX, flat), [])

    def test_the_mesh_and_the_furniture_share_one_read(self):
        md = FakeMD()
        self.bw._with_walkways(None, FRAME, BOX, flat, (0.0, 0.0, 0.0), md)
        calls = md.calls
        rows = walkways.rows(FRAME, md, BOX, flat)
        self.assertEqual(md.calls, calls)
        self.assertTrue(any(r[0] == walkways.BENCH for r in rows))         # the mapped bench in the park

    def test_a_missing_photo_tile_is_not_remembered(self):
        walkways._MEMO.clear()
        scene_walks = [walkways.Walk("x", "walk", np.array([[0.0, 0], [9, 0]]), np.zeros((2, 2)), 2.0, "asphalt", "",
                                     "missing", None, "", 0.0)]
        with mock.patch.object(walkways, "collect", return_value=walkways.Data()),                 mock.patch.object(walkways, "classify", return_value=scene_walks),                 mock.patch("revisit.fault") as fault:
            walkways.tile_data(None, FRAME, BOX)
            walkways.tile_data(None, FRAME, BOX)
        self.assertEqual(fault.call_count, 2)                              # asked again: nothing was kept


class Plan041Test(unittest.TestCase):
    """No yellow pavers without data, untagged sidewalks from their neighbours, pedestrian lines drawn,
    parks cut by cell borders kept whole."""

    def cols(self, n, cls="footway", sub="", surface="", width=None):
        c = {"class": [cls] * n, "subclass": [sub] * n, "osm_surface": [surface] * n, "osm_lit": [""] * n, "name": [""] * n}
        if width is not None:
            c["osm_width"] = [width] * n
        return c

    def run_data(self, rows, cols, land=(), country="UA", bldg=None):
        roads = layer("roads", rows, **cols)
        lu = layer("land_use", [(G, r, f"l{i}") for i, (r, _c) in enumerate(land)],
                   **{"class": [c for _r, c in land], "name": [""] * len(land)}) if land else None
        bl = layer("buildings", [(G, r, f"b{i}") for i, r in enumerate(bldg)]) if bldg else None
        d = walkways.read(FRAME, [roads], [lu] if lu else [], [], BOX, country=country, bldg_list=[bl] if bl else None)
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            return d, {x.fid.split(".")[0]: x for x in walkways.classify(d)}

    def test_untagged_sidewalk_is_asphalt_not_paving(self):
        rows = [(L, [(-60, 5), (60, 5)], "s1")]
        _d, w = self.run_data(rows, self.cols(1, sub="sidewalk"))
        self.assertEqual((w["s1"].role, w["s1"].kind, w["s1"].src), ("sidewalk", "asphalt", "default"))
        self.assertNotEqual(walkways._look(w["s1"])[0], "bne_park_pavers_ua")

    def test_untagged_sidewalk_takes_its_tagged_neighbours(self):
        rows = [(L, [(-60, 5), (60, 5)], "s1"), (L, [(-60, 15), (60, 15)], "s2"), (L, [(-60, 25), (60, 25)], "s3")]
        cols = self.cols(3, sub="sidewalk")
        cols["osm_surface"] = ["", "paving_stones", "paving_stones"]
        _d, w = self.run_data(rows, cols)
        self.assertEqual((w["s1"].kind, w["s1"].src), ("stone", "near"))

    def test_yellow_pavers_only_for_the_paving_stones_tag(self):
        rows = [(L, [(-30, 0), (30, 0)], "a"), (L, [(-30, 20), (30, 20)], "b"), (L, [(-30, 40), (30, 40)], "c")]
        cols = self.cols(3)
        cols["osm_surface"] = ["paving_stones", "paved", ""]
        _d, w = self.run_data(rows, cols)
        self.assertNotEqual(walkways._look(w["a"])[0], "bne_park_pavers_ua")        # (UA pavers are off)
        _d, wc = self.run_data(rows, cols, country="CZ")
        self.assertEqual(walkways._look(wc["a"])[0], "bne_park_pavers_cz")
        self.assertNotEqual(walkways._look(wc["b"])[0], "bne_park_pavers_cz")       # "paved" is asphalt
        self.assertNotEqual(walkways._look(wc["c"])[0], "bne_park_pavers_cz")       # nothing mapped: no colour of ours

    def test_pedestrian_line_in_a_park_is_a_5_m_alley(self):
        rows = [(L, [(-30, 0), (30, 0)], "p")]
        d, w = self.run_data(rows, self.cols(1, cls="pedestrian", surface="compacted"), [(square(0, 0, 100, 60), "park")])
        self.assertEqual((w["p"].role, w["p"].kind, w["p"].width), ("alley", "gravel", walkways.PED_PARK_W))

    def test_pedestrian_line_in_a_town_fills_the_street(self):
        rows = [(L, [(-50, 0), (50, 0)], "p")]
        bldg = [square(0, 10.5, 120, 10), square(0, -10.5, 120, 10)]            # facades 5 m... 16 m apart: 2 x 5.5
        d, w = self.run_data(rows, self.cols(1, cls="pedestrian"), bldg=bldg)
        self.assertEqual(w["p"].role, "walk")
        self.assertEqual(w["p"].kind, "stone")                                  # a pedestrian street is paved
        self.assertAlmostEqual(w["p"].width, (5.5 + 5.5) * walkways.PED_FILL, delta=0.3)
        _d, w2 = self.run_data(rows, self.cols(1, cls="pedestrian", width=7.0), bldg=bldg)
        self.assertEqual(w2["p"].width, 7.0)                                    # a mapped width wins
        _d, w3 = self.run_data(rows, self.cols(1, cls="pedestrian"))            # no facades known: the minimum
        self.assertEqual(w3["p"].width, walkways.PED_TOWN_MIN_W)

    def test_pedestrian_outlines_and_lines_in_a_plaza_are_not_drawn(self):
        t = np.linspace(0, 2 * np.pi, 20)
        ring = [(8 * np.cos(a) + 50, 8 * np.sin(a) + 50) for a in t]
        rows = [(L, ring, "o"), (L, [(-30, 0), (30, 0)], "in")]
        d, w = self.run_data(rows, self.cols(2, cls="pedestrian"), [(square(0, 0, 80, 20), "pedestrian")])
        self.assertNotIn("o", w)                                                 # closed: an area's outline
        self.assertNotIn("in", w)                                                # inside a plaza polygon: plazas.py's

    def test_a_park_cut_by_a_cell_border_is_read_whole(self):
        rows = [(L, [(-30, 0), (30, 0)], "a")]
        pieces = [(G, square(-30, 0, 60, 60), "same"), (G, square(30, 0, 60, 60), "same")]
        roads = layer("roads", rows, **self.cols(1))
        lu1 = layer("land_use", pieces[:1], **{"class": ["park"], "name": [""]})
        lu2 = layer("land_use", pieces[1:], **{"class": ["park"], "name": [""]})
        d = walkways.read(FRAME, [roads, roads], [lu1, lu2], [], BOX)
        self.assertEqual(len(d.parks), 2)
        self.assertTrue(all(abs(a - 7200.0) < 100 for _r, a, _n in d.parks))    # both pieces report the whole 3600 + 3600


class UpstreamLooseTest(unittest.TestCase):
    """With upstream's mapped paths (#971) the gravel and bare strips and pads are theirs."""

    def test_loose_paths_leave_our_strips_but_keep_their_furniture(self):
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            walks = walkways.classify(scene())
        loose = [w for w in walks if w.drawn and w.kind in walkways.LOOSE_KINDS]
        self.assertTrue(loose)
        with mock.patch.object(walkways, "UPSTREAM_LOOSE", True):
            self.assertFalse(any(w.meshed for w in loose))
            self.assertTrue(all(w.drawn for w in loose), "benches, lamps and the clear corridor stay")
            rings = walkways.pad_rings(FRAME, walks, BOX)
            self.assertEqual(rings["loose"], [])
            self.assertGreaterEqual(len(rings["paved"]), 2)
        with mock.patch.object(walkways, "UPSTREAM_LOOSE", False):
            self.assertTrue(all(w.meshed for w in loose))

    def test_the_switch_reads_the_environment(self):
        for val, want in (("on", False), ("off", True)):
            with mock.patch.dict(os.environ, {"BNE_PARKS_LOOSE": val}):
                self.assertEqual(walkways._upstream_paths(), want)

    def test_a_cemetery_is_a_park(self):
        self.assertIn("cemetery", walkways.PARK_CLASSES)


class PadsTest(unittest.TestCase):
    """The ground under the strips: asphalt layer for paved kinds, loose ground for gravel (surface pads)."""

    def walks(self):
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            return walkways.classify(scene())

    def test_rings_by_kind_and_tile_ownership(self):
        walks = self.walks()
        rings = walkways.pad_rings(FRAME, walks, BOX)
        self.assertEqual(len(rings["loose"]), 3)                       # the gravel cross, and the footway beside it
        self.assertGreaterEqual(len(rings["paved"]), 2)                # footway, cycleway, separate sidewalk
        la, lo = rings["loose"][0]
        e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))
        self.assertAlmostEqual(float(np.ptp(np.asarray(e))) * float(np.ptp(np.asarray(n))) > 0, True)
        half = walkways.pad_rings(FRAME, walks, (-100.0, -100.0, 0.0, 100.0))
        other = walkways.pad_rings(FRAME, walks, (0.0, -100.0, 100.0, 100.0))
        self.assertEqual(len(half["loose"]) + len(other["loose"]), 3)  # every walk is one tile's

    def test_ring_is_a_little_wider_than_the_strip(self):
        w = [x for x in self.walks() if x.fid.startswith("a-we")]
        r = walkways.pad_rings(FRAME, w, BOX)["loose"][0]
        e, n, _ = FRAME.geodetic_to_enu(r[0], r[1], np.zeros(len(r[0])))
        self.assertAlmostEqual(float(np.ptp(np.asarray(n))), w[0].width + 2 * walkways.PAD_MARGIN_M, delta=0.05)

    def test_tile_pads_ids(self):
        pads = []
        import roads
        with mock.patch.object(roads, "tile_bbox", return_value=BOX):
            walkways.tile_pads(FRAME, 0, 0, FakeMD(), pads)
        self.assertEqual(sorted(p[0].split(":")[0] for p in pads), ["walkg"])      # the fake cell: a gravel cross only
        pads2 = []
        walkways.tile_pads(FRAME, 0, 0, None, pads2)
        self.assertEqual(pads2, [])

    def test_surface_paints_asphalt_and_loose_apart(self):
        import surface
        sf = surface.Surface(None)
        sf.bus_pads = True
        ring = [(np.array([LAT0, LAT0, LAT0 + 1e-4]), np.array([LON0, LON0 + 1e-4, LON0]))]
        sf.add_fuel_pads([("walk:a", ring), ("walkg:a", ring)])
        la = np.array([LAT0, LAT0 + 2e-4]); lo = np.array([LON0, LON0 + 2e-4])
        self.assertEqual(len(sf.fuel_pads(la, lo)), 1)                  # the asphalt painter: only the paved
        self.assertEqual(len(sf.loose_pads(la, lo)), 1)
        lat, lon = np.meshgrid(np.linspace(LAT0, LAT0 + 1e-4, 20), np.linspace(LON0, LON0 + 1e-4, 20), indexing="ij")
        idx = np.full(lat.shape, surface.GRASS, np.uint8)
        a = surface.paint_pads(idx, lat, lon, sf.fuel_pads(lat, lon))
        b = surface.paint_pads(idx, lat, lon, sf.loose_pads(lat, lon), surface.LOOSE)
        self.assertTrue((a == surface.ASPHALT).any() and not (a == surface.LOOSE).any())
        self.assertTrue((b == surface.LOOSE).any() and not (b == surface.ASPHALT).any())


class Rules031Test(unittest.TestCase):
    """Plan 031: green context, chains and round places, widths by surface, bench budget, lighting, models by country."""

    def data(self, rows, cols, land=(), infra=(), country=None):
        roads = layer("roads", rows, **cols)
        lu = layer("land_use", [(G, r, f"l{i}") for i, (r, _c) in enumerate(land)],
                   **{"class": [c for _r, c in land], "name": [""] * len(land)}) if land else None
        inf = layer("infrastructure", [(P, [xy], f"i{i}") for i, (xy, _c) in enumerate(infra)],
                    **{"class": [c for _xy, c in infra]}) if infra else None
        d = walkways.read(FRAME, [roads], [lu] if lu else [], [inf] if inf else [], BOX, country=country)
        with mock.patch.object(walkways, "_photo", return_value=(None, False)):
            return d, walkways.classify(d)

    def cols(self, n, cls="footway", surface=""):
        return {"class": [cls] * n, "subclass": [""] * n, "osm_surface": [surface] * n, "osm_lit": [""] * n,
                "name": [""] * n}

    def test_path_between_lawns_is_a_square(self):
        rows = [(L, [(-30, 0), (30, 0)], "a")]
        land = [(square(0, 6, 60, 8), "grass"), (square(0, -6, 60, 8), "grass")]
        d, w = self.data(rows, self.cols(1, surface="paving_stones"), land)
        self.assertEqual(w[0].role, "alley")
        self.assertTrue(w[0].group.startswith("g"))
        self.assertAlmostEqual(w[0].park_m2, 960.0, delta=20)
        d2, w2 = self.data(rows, self.cols(1, surface="paving_stones"), land[:1])     # a lawn on one side only: a walk
        self.assertEqual(w2[0].role, "walk")

    def test_a_long_chain_is_an_alley_and_a_small_loop_a_round_place(self):
        t = np.linspace(0, 2 * np.pi, 25)
        circle = [(8 * np.cos(a), 8 * np.sin(a)) for a in t]
        rows = [(L, circle[:13], "r1"), (L, circle[12:], "r2"), (L, [(8, 0), (40, 0)], "s1"), (L, [(40, 0), (80, 0)], "s2"),
                (L, [(-8, 0), (-30, 0)], "s3")]
        d, w = self.data(rows, self.cols(5, surface="gravel"), [(square(0, 0, 190, 60), "park")])
        r = {x.fid.split(".")[0]: x for x in w}
        self.assertTrue(r["r1"].ring and r["r2"].ring)
        self.assertEqual(len(d.rings), 1)
        self.assertAlmostEqual(d.rings[0][1], 8.0, delta=0.3)
        self.assertEqual(r["s1"].role, "alley")                 # s1 + s2 meet alone: a 72 m chain
        self.assertEqual(r["s3"].role, "park")                  # 22 m, gravel, unnamed
        got = walkways.furniture(d, w, BOX)
        round_ = [g for g in got if g[0] in walkways.BENCHES and 8.5 < float(np.hypot(g[1], g[2])) < 12.0]
        self.assertGreaterEqual(len(round_), 3)
        for g in round_:                                         # facing the middle
            self.assertLess(g[1] * g[3] + g[2] * g[4], 0)

    def test_round_place_in_town_is_a_square_with_lamps_outside(self):
        t = np.linspace(0, 2 * np.pi, 33)
        circle = [(9 * np.cos(a), 9 * np.sin(a)) for a in t]
        rows = [(L, circle, "r"), (L, [(9, 0), (40, 0)], "s")]
        with mock.patch.object(walkways, "_urban", return_value=True):
            d, w = self.data(rows, self.cols(2, surface="paving_stones"))
        r = {x.fid.split(".")[0]: x for x in w}
        self.assertTrue(r["r"].ring and r["r"].group == r["s"].group and r["r"].group.startswith("r"))
        got = walkways.furniture(d, w, BOX)
        lamps = [g for g in got if g[0] in walkways.LAMPS]
        self.assertTrue(lamps)
        for g in lamps:
            self.assertGreater(float(np.hypot(g[1], g[2])), 9.0)   # never in the middle (the fountain)
        self.assertGreaterEqual(len([g for g in got if g[0] in walkways.BENCHES]), 3)

    def test_widths_by_surface_and_country(self):
        rows = [(L, [(-30, 0), (30, 0)], "a"), (L, [(-30, 20), (30, 20)], "b"), (L, [(-30, 40), (30, 40)], "c")]
        cols = self.cols(3)
        cols["osm_surface"] = ["paving_stones", "asphalt", "gravel"]
        _d, w = self.data(rows, cols)
        self.assertEqual([x.width for x in w], [3.0, 2.5, 2.5])
        _d, w = self.data(rows, cols, country="CZ")
        self.assertEqual([x.width for x in w], [3.0, 3.0, 3.0])

    def test_bench_budget_by_area(self):
        self.assertGreater(walkways.bench_density(1000), walkways.bench_density(3000))
        self.assertGreater(walkways.bench_density(3000), walkways.bench_density(10000))
        self.assertGreater(walkways.bench_density(10000), walkways.bench_density(50000))
        # a small square: every path of 15 m or more gets a bench; a big park's short park path none
        rows = [(L, [(0, 2), (20, 2)], "a"), (L, [(0, -90), (60, -90)], "b")]
        d, w = self.data(rows, self.cols(2, surface="gravel"), [(square(10, 2, 40, 30), "park"),
                                                                 (square(30, -90, 300, 40), "park")])
        got = walkways.furniture(d, w, BOX)
        near_a = [g for g in got if g[0] in walkways.BENCHES and abs(g[2] - 2) < 4]
        self.assertGreaterEqual(len(near_a), 1)
        self.assertEqual([g for g in got if g[0] in walkways.BENCHES and g[2] < -50], [])     # 60 m park path of 1.2 ha

    def test_benches_come_in_pairs_on_an_alley(self):
        rows = [(L, [(-90, 0), (90, 0)], "a")]
        d, w = self.data(rows, self.cols(1, surface="asphalt"), [(square(0, 0, 200, 40), "park")])
        b = sorted([g for g in walkways.furniture(d, w, BOX) if g[0] in walkways.BENCHES], key=lambda g: g[1])
        self.assertGreaterEqual(len(b), 2)
        gaps = [float(np.hypot(p[1] - q[1], p[2] - q[2])) for p, q in zip(b, b[1:])]
        self.assertAlmostEqual(min(gaps), walkways.BENCH_PAIR_M, delta=0.3)

    def test_town_park_lights_part_of_its_paved_alleys(self):
        rows = [(L, [(-90, y), (90, y)], f"a{i}") for i, y in enumerate((-60, -30, 0, 30, 60))]
        with mock.patch.object(walkways, "_urban", return_value=True):
            d, w = self.data(rows, self.cols(5, surface="asphalt"), [(square(0, 0, 200, 150), "park")])
        lit = walkways.lit_walks(d, w)
        self.assertEqual(len(lit), 2)                           # 40 % of five equal alleys
        with mock.patch.object(walkways, "_urban", return_value=False):
            d, w = self.data(rows, self.cols(5, surface="asphalt"), [(square(0, 0, 200, 150), "park")])
        self.assertEqual(walkways.lit_walks(d, w), set())

    def test_small_town_square_is_lit_whole(self):
        rows = [(L, [(-20, 0), (20, 0)], "a"), (L, [(0, -20), (0, 20)], "b")]
        with mock.patch.object(walkways, "_urban", return_value=True):
            d, w = self.data(rows, self.cols(2, surface="gravel"), [(square(0, 0, 45, 45), "park")])
        self.assertEqual(len(walkways.lit_walks(d, w)), 2)

    def test_models_by_country(self):
        self.assertEqual(walkways.lamp_model("CZ")[0], walkways.LAMP_LANTERN)
        self.assertEqual(walkways.lamp_model("UA")[0], walkways.LAMP_STEEL)
        self.assertEqual(walkways.bench_model("UA")[0], walkways.BENCH_METAL)
        self.assertEqual(walkways.bench_model("CZ")[0], walkways.BENCH)
        self.assertEqual(walkways.country_of(50.53, 16.23), "CZ")
        self.assertEqual(walkways.country_of(49.23, 28.46), "UA")

    def test_pavers_by_country(self):
        rows = [(L, [(-30, 0), (30, 0)], "a"), (L, [(-30, 20), (30, 20)], "b")]
        cols = self.cols(2)
        cols["osm_surface"] = ["paving_stones", "sett"]
        for country, mat in (("CZ", "bne_park_pavers_cz"),):
            _d, w = self.data(rows, cols, country=country)
            self.assertEqual(walkways._look(w[0])[0], mat)
            self.assertNotEqual(walkways._look(w[1])[0], mat)          # a sett street keeps its cobbles
        _d, w = self.data(rows, cols, country="UA")                    # (no yellow pavers anywhere for now)
        self.assertNotEqual(walkways._look(w[0])[0], "bne_park_pavers_ua")
        _d, w = self.data(rows, cols, country="FR")
        self.assertNotIn(walkways._look(w[0])[0], ("bne_park_pavers_ua", "bne_park_pavers_cz"))

    def test_red_cycleways_in_ukraine(self):
        rows = [(L, [(-30, 0), (30, 0)], "a")]
        cols = self.cols(1, cls="cycleway", surface="asphalt")
        _d, w = self.data(rows, cols, country="UA")
        self.assertEqual((w[0].kind, w[0].width), ("cycle_red", 1.8))
        self.assertEqual(walkways._look(w[0])[0], "bne_cycleway_red")
        _d, w = self.data(rows, cols, country="CZ")
        self.assertEqual(w[0].kind, "asphalt")


class InsideTest(unittest.TestCase):
    def test_even_odd(self):
        ring = np.array(square(0, 0, 10, 10))
        self.assertEqual(list(walkways.inside(ring, np.array([[0, 0], [6, 0], [-4.9, 4.9]]))), [True, False, True])


class DeckRoadTest(unittest.TestCase):
    """Overture joins a road into one segment: a bridge on part of it must not make all of it "elevated" (Bukovice: a
    21 m bridge on a 1 km road left the road out of the drives, and a path's lamp stood in the lane of the bridge)."""

    def data(self, spans, lamps=((0.0, 0.5), (-60.0, 20.0))):
        n = 1
        cols = {"class": ["secondary"], "osm_sidewalk": [""], "name": [""], "subclass": [""], "osm_surface": [""],
                "osm_lit": [""]}
        roads = layer("roads", [(L, [(-100, 0), (100, 0)], "z-main")], **cols)
        if spans:
            roads.rules = {"level": mapgeom.RuleTable(
                np.zeros(len(spans), np.int32), np.array(["1.0"] * len(spans)),
                np.array([a for a, _b in spans], np.float32), np.array([b for _a, b in spans], np.float32))}
        land = layer("land_use", [(G, square(0, 10, 200, 40), "p1")], **{"class": ["park"], "name": ["Sad"]})
        infra = layer("infrastructure", [(P, [xy], "l%d" % i) for i, xy in enumerate(lamps)],
                      **{"class": ["street_lamp"] * len(lamps)})
        return walkways.read(FRAME, [roads], [land], [infra], BOX)

    def test_a_bridge_part_is_a_deck_and_the_rest_a_drive(self):
        d = self.data([(0.45, 0.55)])
        self.assertEqual(len(d.drives), 2)
        self.assertEqual(len(d.decks), 1)
        lengths = sorted(float(np.hypot(*np.diff(xy, axis=0).T).sum()) for xy, _h, _s in d.drives)
        self.assertAlmostEqual(lengths[0], 90.0, delta=0.5)
        self.assertAlmostEqual(lengths[1], 90.0, delta=0.5)
        self.assertAlmostEqual(float(np.hypot(*np.diff(d.decks[0][0], axis=0).T).sum()), 20.0, delta=0.5)
        self.assertAlmostEqual(d.decks[0][1], walkways.DRIVE_HALF_M["secondary"])

    def test_a_road_with_no_level_rule_is_all_drive(self):
        d = self.data([])
        self.assertEqual((len(d.drives), len(d.decks)), (1, 0))

    def test_a_road_on_a_viaduct_all_along_is_all_deck(self):
        d = self.data([(0.0, 1.0)])
        self.assertEqual((len(d.drives), len(d.decks)), (0, 1))

    def test_touching_spans_are_one_deck(self):
        d = self.data([(0.2, 0.4), (0.4, 0.6)])
        self.assertEqual((len(d.drives), len(d.decks)), (2, 1))

    def test_a_mapped_lamp_on_the_deck_is_left_out_and_one_off_the_road_stays(self):
        d = self.data([(0.45, 0.55)])
        got = [(round(x), round(y)) for g, x, y, *_ in walkways.furniture(d, walkways.classify(d), BOX)]
        self.assertNotIn((0, 0), got, "on the bridge")
        self.assertIn((-60, 20), got, "15 m off the road")

    def test_a_mapped_lamp_beside_the_ground_part_is_left_out_too(self):
        d = self.data([(0.45, 0.55)], lamps=((-60.0, 0.5),))
        self.assertEqual(walkways.furniture(d, walkways.classify(d), BOX), [])


if __name__ == "__main__":
    unittest.main()
