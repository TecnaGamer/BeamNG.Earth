"""Hiking guideposts, information boards and memorials of localpoi.py / localpoisign.py (stages D, E and F)."""
import json
import math
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast
import geodesy
import localpoi
import localpoisign
import mapgeom
import picnic
import roads


def _post(fid, name="", x=0.0, y=0.0, hiking=True, bicycle=False, ele="", kind="guidepost", **more):
    g = {"fid": fid, "kind": kind, "x": x, "y": y, "name": name, "ele": ele, "ref": "", "hiking": hiking,
         "bicycle": bicycle, "board_type": "", "description": "", "direction": "", "operator": ""}
    g.update(more)
    return g


class TargetsTest(unittest.TestCase):
    def test_distance_text(self):
        self.assertEqual(localpoisign.distance_text(412.0), "400 m")
        self.assertEqual(localpoisign.distance_text(870.0), "850 m")
        self.assertEqual(localpoisign.distance_text(3480.0), "3,5 km")
        self.assertEqual(localpoisign.distance_text(3480.0, "GB"), "3.5 km")
        self.assertEqual(localpoisign.distance_text(960.0), "1,0 km")

    def test_nearest_named_posts_in_different_directions(self):
        me = _post("a", "Home")
        others = [me,
                  _post("b", "East", 500.0, 0.0), _post("c", "East2", 900.0, 30.0),       # same direction as East
                  _post("d", "North", 0.0, 800.0), _post("e", "West", -1200.0, 0.0),
                  _post("f", "FarSouth", 0.0, -3000.0), _post("g", "", 300.0, 300.0),     # too far, no name
                  _post("h", "Home", 700.0, 700.0)]                                         # the same name
        got = localpoisign.pick_targets(me, others)
        self.assertEqual([t[0] for t in got], ["East", "North", "West"])
        self.assertAlmostEqual(got[0][1], 500.0 * localpoisign.WIND)
        self.assertAlmostEqual(got[0][2][0], 1.0)
        self.assertEqual(len(localpoisign.pick_targets(me, others)), localpoisign.MAX_ARROWS)

    def test_a_cycling_post_points_at_cycling_posts_only_and_a_close_one_is_skipped(self):
        me = _post("a", "Home", hiking=False, bicycle=True)
        others = [me, _post("b", "Hike", 400.0, 0.0), _post("c", "Bike", 0.0, 500.0, hiking=False, bicycle=True),
                  _post("d", "Next door", 40.0, 0.0, hiking=False, bicycle=True),
                  _post("e", "A board", 0.0, -300.0, kind="board", hiking=False, bicycle=True)]
        self.assertEqual([t[0] for t in localpoisign.pick_targets(me, others)], ["Bike"])
        self.assertEqual(localpoisign.family(me), "bike")
        self.assertEqual(localpoisign.family(_post("z", hiking=False)), "hike")

    def test_board_title(self):
        g = _post("b", "Skalni mesto", kind="board", board_type="nature", description="Naucna stezka")
        self.assertEqual(localpoisign.board_title(g, "CZ"), ("Skalni mesto", "Naucna stezka"))
        self.assertEqual(localpoisign.board_title(_post("m", "", kind="map"), "CZ")[0], "Mapa")
        self.assertEqual(localpoisign.board_title(_post("m", "", kind="map"), "XX")[0], "Map")
        long = _post("b", "T", kind="board", description="x" * 80)
        self.assertEqual(localpoisign.board_title(long, "CZ")[1], "")

    def test_ele_text(self):
        self.assertEqual(localpoisign._ele_text("550"), "550 m")
        self.assertEqual(localpoisign._ele_text("524.7"), "525 m")
        self.assertEqual(localpoisign._ele_text(""), "")
        self.assertEqual(localpoisign._ele_text("high"), "")


class HangTest(unittest.TestCase):
    """The rows of boards hung on a tube, through the kernel's mesh: where the picture lands."""

    def picture(self, bearing, w=0.4, h=0.2):
        fast.require(localpoisign.KERNEL, "test")
        b = (math.cos(math.radians(bearing)), math.sin(math.radians(bearing)))
        f = (b[1], -b[0])
        row = localpoisign.hang_row((0.0, 0.0), f, w / 2.0, w, h, 1, 2.0, 0.0)
        p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray([row], np.float64), [0.0, 0.0, 0.0])
        p, t, uv, mi = np.asarray(p), np.asarray(t), np.asarray(uv), np.asarray(mi)
        face = [k for k in range(len(t)) if mi[k] == 1]
        tri = p[t[face]]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]).mean(0)
        n = n[:2] / np.linalg.norm(n[:2])
        pts = tri.reshape(-1, 3)
        uvs = uv[t[face]].reshape(-1, 2)
        return b, f, n, pts[np.argmin(uvs[:, 0])][:2], pts[np.argmax(uvs[:, 0])][:2]

    def test_the_picture_starts_at_the_tube_and_reaches_to_the_target(self):
        for bearing in (0, 35, 90, 200, 300):
            b, f, n, left, right = self.picture(bearing)
            self.assertLess(float(np.dot(n, f)), 1.0 + 1e-6)
            self.assertGreater(float(np.dot(n, f)), 0.99, f"faces the reader at {bearing}")
            reach = np.array(right) - np.array(left)
            self.assertGreater(float(np.dot(reach, b)), 0.35, f"reaches along the bearing at {bearing}")
            self.assertLess(abs(float(np.dot(left, b))), 0.08, f"its left end is at the tube at {bearing}")


class MemorialTest(unittest.TestCase):
    def test_the_model_by_the_description(self):
        mg = localpoi.memorial_group
        self.assertEqual(mg("w1", "memorial", "Socha svatého Jana Nepomuckého v Polici nad Metují"), localpoi.G_STATUE)
        self.assertEqual(mg("w1", "memorial", "Pomník padlým v první světové válce"), localpoi.G_STELE)
        self.assertEqual(mg("w1", "memorial", "Pamětní deska Jiřího Vostrého"), localpoi.G_BOULDER)
        self.assertEqual(mg("w1", "monument", "obelisk"), localpoi.G_OBELISK)
        self.assertEqual(mg("w1", "stone", "Smírčí kříž v Bezděkově"), localpoi.G_STONE_CROSS)
        self.assertEqual(mg("w1", "stone", ""), localpoi.G_BOULDER)
        self.assertEqual(mg("w1", "memorial", "Pomnik ofiar wojny"), localpoi.G_STELE)

    def test_no_description_is_stable_and_both_models_occur(self):
        got = {localpoi.memorial_group(f"id{i}", "memorial", "") for i in range(60)}
        self.assertEqual(got, {localpoi.G_STELE, localpoi.G_OBELISK})
        self.assertEqual(localpoi.memorial_group("x", "memorial", ""), localpoi.memorial_group("x", "memorial", ""))


def _infra(frame, items):
    """items: (id, name, tags, x, y): Overture-style infrastructure points (class information)."""
    n = len(items)
    lonlat = []
    for _fid, _name, _tags, x, y in items:
        la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), np.array([0.0]))
        lonlat.append([lo[0], la[0]])
    return mapgeom.Features(
        layer="infrastructure", ids=np.array([i[0] for i in items]), kind=np.full(n, mapgeom.POINT, np.uint8),
        part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64),
        coord_off=np.arange(n + 1, dtype=np.int64), lonlat=np.array(lonlat, np.float64).reshape(-1, 2),
        attrs={"class": np.array(["information"] * n), "subtype": np.array(["pedestrian"] * n),
               "name": np.array([i[1] for i in items]), "tags": np.array([json.dumps(i[2]) for i in items])})


def _ways(frame, lines):
    """lines: [[(x, y), ...]]: road centre lines as a roads layer."""
    ids, lonlat, coord_off = [], [], [0]
    for k, line in enumerate(lines):
        ids.append(f"r{k}")
        xy = np.asarray(line, np.float64)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        lonlat.append(np.column_stack((lo, la)))
        coord_off.append(coord_off[-1] + len(xy))
    n = len(lines)
    return mapgeom.Features(
        layer="roads", ids=np.array(ids), kind=np.full(n, mapgeom.LINE, np.uint8),
        part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64),
        coord_off=np.array(coord_off, np.int64), lonlat=np.vstack(lonlat), attrs={"class": np.array(["path"] * n)})


class StubCell:
    def __init__(self, layers):
        self.layers = layers


class StubMd:
    def __init__(self, **layers):
        self.layers = layers

    def cell(self, key, layers=(), kind="demand"):
        return StubCell({name: self.layers.get(name) for name in layers})


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(50.5267522, 16.2718353, 0.0)
        self.items = [("n1", "Barešův kříž", {"information": "guidepost", "ele": "550", "bicycle": "yes"}, 100.0, 100.0),
                      ("n2", "Slavný", {"information": "guidepost", "bicycle": "yes"}, 700.0, 100.0),
                      ("n3", "Suchý Důl", {"information": "guidepost", "bicycle": "yes"}, 100.0, 900.0),
                      ("b1", "Broumovské stěny", {"information": "board", "board_type": "nature", "direction": "90"},
                       150.0, 150.0),
                      ("m1", "", {"information": "map"}, 180.0, 100.0)]

    def infra(self):
        feats = _infra(self.frame, self.items)
        return lambda key: feats

    def test_guideposts_read_name_tags_and_box(self):
        got = localpoi.guideposts(self.frame, self.infra(), (0.0, 0.0, 256.0, 256.0))
        self.assertEqual([g["fid"] for g in got], ["b1", "m1", "n1"])
        post = [g for g in got if g["fid"] == "n1"][0]
        self.assertEqual((post["name"], post["ele"], post["bicycle"], post["hiking"]), ("Barešův kříž", "550", True, False))
        self.assertEqual([g["kind"] for g in got], ["board", "map", "guidepost"])
        wide = localpoi.guideposts(self.frame, self.infra(), (0.0, 0.0, 256.0, 256.0), pad_m=2000.0)
        self.assertEqual(len(wide), 5)

    def test_the_post_is_a_prop_and_not_inside_a_building(self):
        posts = localpoi.guideposts(self.frame, self.infra(), (0.0, 0.0, 256.0, 256.0))
        rows = localpoi.guidepost_rows(posts, (0.0, 0.0, 256.0, 256.0), lambda la, lo: np.full(np.shape(la), 300.0),
                                       self.frame)
        self.assertEqual([r[0] for r in rows], [localpoi.G_GP])
        self.assertEqual(rows[0][6], localpoi.HEIGHT[localpoi.G_GP])
        self.assertAlmostEqual(rows[0][1], 100.0, delta=0.05)
        inside = [(30.0, 30.0, 40.0, 40.0, np.array([[0, 0], [250, 0], [250, 250], [0, 250], [0, 0.0]]), [])]
        self.assertEqual(localpoi.guidepost_rows(posts, (0.0, 0.0, 256.0, 256.0), lambda la, lo: np.zeros(np.shape(la)),
                                                 self.frame, covers=[(0.0, 250.0, 0.0, 250.0, inside[0][4], [])]), [])

    def test_toward_way(self):
        road = localpoi.Ways(np.array([[0.0, 0.0, 0.0]]), np.array([[100.0, 0.0, 0.0]]))
        self.assertEqual(localpoi.toward_way(road, 50.0, 10.0), (0.0, -1.0))
        self.assertEqual(localpoi.toward_way(road, 50.0, -10.0), (0.0, 1.0))
        self.assertIsNone(localpoi.toward_way(road, 50.0, 80.0))
        self.assertIsNone(localpoi.toward_way(None, 0.0, 0.0))

    def test_ways_near_reads_the_roads_layer(self):
        lines = _ways(self.frame, [[(0.0, 120.0), (250.0, 120.0)]])
        ways = localpoi.ways_near(self.frame, lambda key: lines, (0.0, 0.0, 256.0, 256.0))
        self.assertGreaterEqual(len(ways.a), 1)       # (the stub gives the same layer for each map cell it spans)
        self.assertAlmostEqual(localpoi.toward_way(ways, 100.0, 100.0)[1], 1.0, places=3)

    def test_the_sign_tile_makes_plates_arrows_and_boards(self):
        md = StubMd(infrastructure=_infra(self.frame, self.items))
        with tempfile.TemporaryDirectory() as d:
            elev = lambda la, lo: np.full(np.shape(la), 300.0)
            mesh, faces = localpoisign.sign_tile(md, self.frame, 0, 0, elev, d, "/v", country="CZ")
            self.assertIsNotNone(mesh)
            p, t, uv, materials, mi = mesh
            self.assertEqual(materials[0], "bne_dir_metal")
            # the plate and two arrows of the one post here (n2 and n3 are the tile's neighbours, not in it), the board
            names = sorted(m.rsplit("_", 1)[0] for m in faces)
            self.assertEqual(names.count("bne_lpp"), 1)
            self.assertEqual(names.count("bne_lpa"), 2)
            self.assertEqual(names.count("bne_lpb"), 2)       # the board and the map
            for path in faces.values():
                self.assertTrue(os.path.exists(os.path.join(d, os.path.basename(path))))
            self.assertEqual(len(materials), 1 + len(faces))
            self.assertTrue(set(np.asarray(mi).tolist()) <= set(range(len(materials))))
            # the same tile again: the same pictures
            again = localpoisign.sign_tile(md, self.frame, 0, 0, elev, d, "/v", country="CZ")
            self.assertEqual(sorted(again[1]), sorted(faces))

    def test_a_tile_with_none_makes_nothing(self):
        md = StubMd(infrastructure=_infra(self.frame, self.items))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(localpoisign.sign_tile(md, self.frame, 5, 5, lambda la, lo: np.zeros(np.shape(la)), d, "/v"),
                             (None, {}))

    def test_off_switch(self):
        md = StubMd(infrastructure=_infra(self.frame, self.items))
        localpoi.ON = False
        try:
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(localpoisign.sign_tile(md, self.frame, 0, 0, lambda la, lo: np.zeros(np.shape(la)), d,
                                                        "/v"), (None, {}))
            self.assertEqual(localpoi.rows(self.frame, lambda key: _infra(self.frame, self.items), None,
                                           (0.0, 0.0, 256.0, 256.0), lambda la, lo: np.zeros(np.shape(la))), [])
        finally:
            localpoi.ON = True

    def test_signstream_and_catalog_know_them(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "server", "signstream.py"), encoding="utf-8") as f:
            self.assertIn("localpoisign.sign_tile", f.read())
        with open(os.path.join(root, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua"),
                  encoding="utf-8") as f:
            lua = f.read()
        shapes = os.path.join(root, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                              "localpoi")
        for group, model in (("lp_gp", "bne_lp_gp_post"), ("lp_stele", "bne_lp_stele"), ("lp_obelisk", "bne_lp_obelisk"),
                             ("lp_boulder", "bne_lp_boulder"), ("lp_statue", "bne_lp_statue"),
                             ("lp_stone_cross", "bne_lp_stone_cross")):
            self.assertIn(f"M.CATALOG.{group} = {{ {{ LP .. '{model}', {localpoi.HEIGHT[group]} }} }}", lua)
            self.assertTrue(os.path.exists(os.path.join(shapes, model + ".dae")), model)


if __name__ == "__main__":
    unittest.main()
