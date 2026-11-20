"""Hong Kong's street lamps (hklamps.py): the Highways Department's points as mapped street lamps in worldprops.tile_props.
Made-up points only (a fake of the government's ArcGIS layer), nothing of the real data."""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geodesy  # noqa: E402
import hklamps  # noqa: E402
import hklidar  # noqa: E402
import mapcells  # noqa: E402
import mapgeom as mg  # noqa: E402
import revisit  # noqa: E402
import worldprops as wp  # noqa: E402
from test_roads import features  # noqa: E402

HK = geodesy.EnuFrame(22.3000, 114.1700)      # (Kowloon: a made-up frame)
PRAGUE = geodesy.EnuFrame(50.0870, 14.4208)
UA = {"BNE_HK_LAMPS": "on"}


def lonlat(frame, xy):
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.stack([lo, la], 1)


def elev(la, lo):
    return np.full(np.shape(la), 5.0)


class Layer:
    """The government's layer, made up: points (lon, lat, number); get() answers an envelope query by its pages."""

    def __init__(self, pts):
        self.pts, self.urls = list(pts), []

    def get(self, url, kind="demand"):
        self.urls.append(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        w, s, e, n = (float(v) for v in q["geometry"].split(","))
        rows = [p for p in self.pts if w <= p[0] <= e and s <= p[1] <= n]       # (esri intersects: the edges count)
        off, cnt = int(q["resultOffset"]), int(q["resultRecordCount"])
        feats = [{"attributes": {"OBJECTID": 100 + i, "LAMP_NO": p[2]}, "geometry": {"x": p[0], "y": p[1]}}
                 for i, p in enumerate(rows)][off:off + cnt]
        return SimpleNamespace(body=json.dumps({"features": feats, "exceededTransferLimit": off + cnt < len(rows)}).encode())


class Down:
    def __init__(self):
        self.urls = []

    def get(self, url, kind="demand"):
        self.urls.append(url)
        raise OSError("offline")


def points_at(frame, xy, numbers):
    ll = lonlat(frame, xy)
    return [(float(a), float(b), n) for (a, b), n in zip(ll, numbers)]


def roads_layer(lines, classes, flags=()):
    """A roads layer round HK: lines [(x, y), ...] in the frame (test_roads' features, moved to the frame's place)."""
    f = features(lines, classes, flags=flags)
    f.lonlat[:] = np.concatenate([lonlat(HK, ln) for ln in lines])
    return f


def lamp_layer(rows):
    """An infrastructure layer of OpenStreetMap lamp nodes at (x, y)s."""
    f = mg.build("infrastructure", np.array([f"i{k}" for k in range(len(rows))]),
                 [(mg.POINT, [[lonlat(HK, [xy])]]) for xy in rows])
    f.attrs["class"] = np.array(["street_lamp"] * len(rows), object)
    f.attrs["src_record"] = np.array([f"n{900 + k}@1" for k in range(len(rows))], object)
    return f


class MD:
    """The map data of a tile: layers by name, and the lamps' source when given."""

    def __init__(self, layers, source=None):
        self.layers = layers
        self.hklamps = source

    def cell(self, key, layers=None, kind="demand"):
        return SimpleNamespace(layers={k: self.layers.get(k) for k in (layers or ())})


class ParseTest(unittest.TestCase):
    def test_parse_keeps_the_cells_own_points_ordered(self):
        f = [{"attributes": {"OBJECTID": 7, "LAMP_NO": " AB12 "}, "geometry": {"x": 114.17, "y": 22.30}},
             {"attributes": {"OBJECTID": 8, "LAMP_NO": None}, "geometry": {"x": 114.171, "y": 22.30}},
             {"attributes": {"OBJECTID": 9, "LAMP_NO": "AA01"}, "geometry": {"x": 114.18, "y": 22.30}},      # (east edge)
             {"attributes": {"OBJECTID": 10, "LAMP_NO": "AA02"}, "geometry": {"x": 114.17, "y": 22.31}},     # (north edge)
             {"attributes": {"OBJECTID": 11, "LAMP_NO": "AA03"}, "geometry": {"x": 114.172}},                # (no place)
             {"attributes": {"OBJECTID": 12, "LAMP_NO": "AA04"}, "geometry": {"x": "x", "y": 22.30}},
             {"attributes": {"OBJECTID": 13, "LAMP_NO": "AA05"}, "geometry": {"x": float("nan"), "y": 22.30}},
             "junk", {"attributes": None, "geometry": None}]
        got = hklamps.parse(f, (114.17, 22.29, 114.18, 22.31))
        self.assertEqual(list(got["no"]), ["AB12", "o8"])
        self.assertEqual(list(got["lon"]), [114.17, 114.171])
        self.assertEqual(hklamps.parse([], (0, 0, 1, 1))["lon"].shape, (0,))
        self.assertEqual(hklamps.parse(None, (0, 0, 1, 1))["no"].shape, (0,))
        shuffled = hklamps.parse(f[::-1], (114.17, 22.29, 114.18, 22.31))
        self.assertEqual(list(shuffled["no"]), ["AB12", "o8"], "the page order is no part of the records")


class SourceTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.key = mapcells.cells_for_bbox(22.30, 114.17, 22.30, 114.17)[0]
        w, s, e, n = self.key.bounds()
        self.pts = [(w + 0.001, s + 0.001, "AA0001"), (w + 0.002, s + 0.003, "AA0002"), (e, s + 0.001, "AA0003")]

    def test_a_cell_is_fetched_once_kept_and_read_back(self):
        net = Layer(self.pts)
        src = hklamps.Source(self.dir, net)
        got = src.load(self.key)
        self.assertEqual((got.n, sorted(got.no)), (2, ["AA0001", "AA0002"]), "the east edge is the next cell's")
        self.assertEqual(len(net.urls), 1)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(net.urls[0]).query).items()}
        self.assertEqual((q["inSR"], q["outSR"], q["outFields"], q["orderByFields"]),
                         ("4326", "4326", "OBJECTID,LAMP_NO", "OBJECTID"))
        self.assertTrue(net.urls[0].startswith(hklamps.URL))
        path = os.path.join(self.dir, "hklamps", "v1", str(self.key.z), str(self.key.x), f"{self.key.y}.npz")
        self.assertTrue(os.path.exists(path))
        again = hklamps.Source(self.dir, None).cached(self.key)          # (a new process: from disk, no network)
        self.assertEqual((again.n, list(again.no), list(again.lon)), (got.n, list(got.no), list(got.lon)))
        self.assertEqual(src.wait([self.key]), True)
        self.assertEqual(len(net.urls), 1)
        self.assertTrue(hklamps.credited_near(self.dir, 22.30, 114.17, 500.0))
        self.assertFalse(hklamps.credited_near(self.dir, 50.087, 14.42, 500.0))

    def test_paged_answers_join(self):
        net = Layer(self.pts)
        with mock.patch.object(hklidar, "PAGE", 1):
            got = hklamps.Source(self.dir, net).load(self.key)
        self.assertEqual((got.n, len(net.urls)), (2, 2), "a page of one point, then the one that ends the answer")

    def test_a_cell_without_lamps_is_kept_empty_and_not_credited(self):
        src = hklamps.Source(self.dir, Layer([]))
        self.assertEqual(src.load(self.key).n, 0)
        self.assertEqual(hklamps.Source(self.dir, None).cached(self.key).n, 0)
        self.assertFalse(hklamps.credited_near(self.dir, 22.30, 114.17, 500.0))

    def test_a_failed_download_is_not_kept_nor_asked_again_at_once(self):
        net = Down()
        src = hklamps.Source(self.dir, net)
        self.assertIsNone(src.load(self.key))
        self.assertIsNone(src.cached(self.key))
        n = len(net.urls)
        self.assertEqual(src.prefetch([self.key]), [])
        self.assertEqual(len(net.urls), n)
        self.assertFalse(src.wait([self.key]))

    def test_a_saved_area_takes_the_cells_lamps_with_its_infrastructure(self):
        import mapdata
        net = Layer(self.pts)
        stub = SimpleNamespace(hklamps=hklamps.Source(self.dir, net), path=lambda key, layer, parquet=False: __file__)
        self.assertFalse(mapdata.MapData.predownload(stub, self.key, "roads"))
        self.assertEqual(net.urls, [])
        self.assertFalse(mapdata.MapData.predownload(stub, self.key, "infrastructure"))      # (its file is there: nothing built)
        self.assertEqual((len(net.urls), stub.hklamps.cached(self.key).n), (1, 2))

    def test_nothing_is_asked_outside_hong_kong_or_when_off(self):
        net = Layer(self.pts)
        src = hklamps.Source(self.dir, net)
        prague = mapcells.cells_for_bbox(50.087, 14.4208, 50.087, 14.4208)[0]
        self.assertEqual(src.prefetch([prague]), [])
        self.assertTrue(src.wait([prague]))
        self.assertEqual(src.load(prague).n, 0)
        with mock.patch.object(hklamps, "ON", False):
            self.assertEqual(src.prefetch([self.key]), [])
            self.assertTrue(src.wait([self.key]))
        self.assertEqual(net.urls, [])


def net_of(*pieces):
    """A Net of pieces (polyline [(x, y), ...], half width, kind)."""
    out = []
    for line, half, kind in pieces:
        p = np.asarray(line, np.float64)
        out.append((p[:-1], p[1:], half, kind))
    return hklamps.Net(out)


STREET = ([(-100., 0.), (200., 0.)], 4.0, hklamps.K_GROUND)         # 8 m wide, along y = 0


class ChooseTest(unittest.TestCase):
    def choose(self, xy, osm=(), *pieces):
        return hklamps.choose(np.asarray(xy, np.float64), osm, net_of(*pieces) if pieces else hklamps.Net([]))

    def test_an_openstreetmap_lamp_within_5_m_wins(self):
        xy = [[0., 20.], [10., 20.], [20., 20.]]
        why, _, _ = self.choose(xy, [(3., 24.), (14.9, 20.), (45., 20.)], STREET)
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.OSM, hklamps.KEPT], "5 m exactly is not within; 4.9 is")

    def test_points_on_a_bridge_or_in_a_tunnel_are_left_to_their_road(self):
        deck = ([(0., 0.), (200., 0.)], 5.0, hklamps.K_BRIDGE)       # 10 m wide
        tube = ([(0., 100.), (200., 100.), (200., 300.)], 4.0, hklamps.K_TUNNEL)
        xy = [[100., 4.],      # on the deck
              [120., 7.],      # 7 m: within half width + FOOT_PAD_M
              [140., 7.5],     # beside it: the ground
              [201., 2.],      # past its end by a metre, within the pad of the end
              [100., 103.],    # in the tube
              [203., 200.],    # in the tube's second leg
              [100., 20.]]     # clear of both, beside a road
        why, out, moved = self.choose(xy, (), deck, tube, ([(0., 20.), (400., 20.)], 3.0, hklamps.K_GROUND),
                                      ([(0., 400.), (400., 400.)], 3.0, hklamps.K_GROUND), ([(250., 0.), (250., 400.)], 3.0, hklamps.K_GROUND))
        self.assertEqual(why.tolist(), [hklamps.BRIDGE, hklamps.BRIDGE, 0, hklamps.BRIDGE, hklamps.TUNNEL, hklamps.TUNNEL, 0])

    def test_a_deck_over_another_road_takes_the_lamps_under_it_too(self):
        # the nearest road of such a point is the road under the deck: the deck's footprint decides
        deck = ([(0., 0.), (200., 0.)], 5.0, hklamps.K_BRIDGE)
        why, _, _ = self.choose([[100., 3.]], (), deck, ([(100., -50.), (100., 50.)], 3.0, hklamps.K_GROUND))
        self.assertEqual(why.tolist(), [hklamps.BRIDGE])

    def test_a_tunnel_under_a_deck_is_a_tunnel(self):
        why, _, _ = self.choose([[50., 1.]], (), ([(0., 0.), (200., 0.)], 5.0, hklamps.K_BRIDGE),
                                ([(0., 0.), (200., 0.)], 4.0, hklamps.K_TUNNEL))
        self.assertEqual(why.tolist(), [hklamps.TUNNEL])

    def test_a_point_in_a_carriageway_moves_straight_out_to_where_a_guessed_lamp_stands(self):
        import furniture
        out_to = 4.0 + furniture.LAMP_EDGE_M
        xy = [[10., 1.0],        # in the carriageway, north of the centreline
              [20., -2.0],       # in it, south
              [30., 4.1],        # at the kerb: inside by the margin
              [40., 4.3],        # 0.3 m past the margin: stays
              [50., 7.5],        # on the sidewalk: stays where the department has it
              [60., 0.0]]        # on the centreline: out to the left of the road's direction
        why, out, moved = self.choose(xy, (), STREET)
        self.assertEqual(why.tolist(), [hklamps.KEPT] * 6)
        self.assertEqual(moved.tolist(), [True, True, True, False, False, True])
        np.testing.assert_allclose(out[[0, 1, 2, 5]], [[10., out_to], [20., -out_to], [30., out_to], [60., out_to]], atol=1e-9)
        np.testing.assert_array_equal(out[[3, 4]], np.array(xy)[[3, 4]])
        self.assertTrue(hklamps.MARGIN_M < 0.3)

    def test_a_point_moved_into_another_carriageway_is_dropped_at_a_junction(self):
        side = ([(30., -100.), (30., 100.)], 3.0, hklamps.K_GROUND)       # a street across it, 6 m wide
        xy = [[10., 1.0],        # moves out to (10, 5.2): clear of the cross street
              [28., 1.0],        # would stand at (28, 5.2): in the cross street's carriageway
              [31., 1.0]]
        why, out, moved = self.choose(xy, (), STREET, side)
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.BLOCKED, hklamps.BLOCKED])
        self.assertEqual(moved.tolist(), [True, True, True])

    def dual(self, gap):
        """Two one-way carriageways 7 m wide: A along y = 0, B along y = 3.5 + gap + 3.5 (a median `gap` m wide)."""
        a = ([(-100., 0.), (200., 0.)], 3.5, hklamps.K_GROUND)
        b = ([(-100., 7. + gap), (200., 7. + gap)], 3.5, hklamps.K_GROUND)
        return a, b

    def test_a_median_wide_enough_for_the_near_side_is_not_touched_by_the_new_steps(self):
        why, out, moved = self.choose([[20., 1.0]], (), *self.dual(3.0))
        self.assertEqual((why.tolist(), moved.tolist()), ([hklamps.KEPT], [hklamps.NEAR_SIDE]))
        np.testing.assert_allclose(out, [[20., 4.7]], atol=1e-9)

    def test_a_narrow_median_gets_its_lamp_in_the_middle_of_it(self):
        why, out, moved = self.choose([[20., 1.0], [60., 7.5]], (), *self.dual(1.2))
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.KEPT])
        self.assertEqual(moved.tolist(), [hklamps.MEDIAN, hklamps.MEDIAN])
        # 1.2 m of median (y 3.5..4.7): the middle, 0.6 m from both kerbs, from either side
        np.testing.assert_allclose(out, [[20., 4.1], [60., 4.1]], atol=1e-9)

    def test_a_median_of_one_metre_or_less_has_no_room_for_a_lamp_and_the_post_goes_to_the_far_side(self):
        why, out, moved = self.choose([[20., 1.0]], (), *self.dual(0.9))
        self.assertEqual((why.tolist(), moved.tolist()), ([hklamps.KEPT], [hklamps.FAR_SIDE]))
        np.testing.assert_allclose(out, [[20., -4.7]], atol=1e-9)

    def test_a_post_with_no_room_on_either_side_is_not_drawn(self):
        # a cross street across the far side too, and a median too narrow
        a, b = self.dual(0.9)
        cross = ([(20., -100.), (20., 100.)], 3.0, hklamps.K_GROUND)
        why, out, moved = self.choose([[20., 1.0]], (), a, b, cross)
        self.assertEqual(why.tolist(), [hklamps.BLOCKED])

    def test_the_median_is_not_a_footprint(self):
        # a deck over the median: the median spot is on it, the far side is free
        a, b = self.dual(1.2)
        deck = ([(-100., 4.1), (200., 4.1)], 0.5, hklamps.K_BRIDGE)
        why, out, moved = self.choose([[20., 1.0]], (), a, b, deck)
        self.assertEqual((why.tolist(), moved.tolist()), ([hklamps.KEPT], [hklamps.FAR_SIDE]))

    def test_a_moved_post_within_6_m_of_a_kept_one_is_dropped_the_kept_one_stays(self):
        a, b = self.dual(0.9)
        far_post = [20., -6.5]                       # a surveyed post on the outer verge, 2.4 m from where this one would stand
        xy = [[20., 1.0], far_post, [40., 1.0], [47., -6.5], [70., 1.0]]
        why, out, moved = self.choose(xy, (), a, b)
        # [20, 1] -> far side (20, -4.7): 1.8 m from the surveyed post: dropped. [40, 1] -> (40, -4.7), 7.4 m from
        # (47, -6.5): kept. [70, 1] -> (70, -4.7): alone.
        self.assertEqual(why.tolist(), [hklamps.DOUBLED, hklamps.KEPT, hklamps.KEPT, hklamps.KEPT, hklamps.KEPT])
        np.testing.assert_array_equal(out[[1, 3]], np.array(xy)[[1, 3]])
        self.assertEqual(moved.tolist(), [hklamps.FAR_SIDE, 0, hklamps.FAR_SIDE, 0, hklamps.FAR_SIDE])

    def test_of_two_moved_posts_within_6_m_the_earlier_stays(self):
        a, b = self.dual(1.2)
        why, out, moved = self.choose([[20., 1.0], [24., 7.5]], (), a, b)             # both to the middle, 4 m apart
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.DOUBLED])
        why, _, _ = self.choose([[24., 7.5], [20., 1.0]], (), a, b)
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.DOUBLED], "the earlier in the order given stays")

    def test_the_near_side_moves_are_not_judged_by_the_6_m_rule(self):
        # two posts 3 m apart, both moved out to the near side by a street without a median: both stay as they were before
        why, out, moved = self.choose([[10., 1.0], [13., 1.0]], (), STREET)
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.KEPT])
        self.assertEqual(moved.tolist(), [hklamps.NEAR_SIDE, hklamps.NEAR_SIDE])

    def test_a_point_with_no_road_within_40_m_is_not_drawn(self):
        xy = [[10., 39.], [20., 41.], [300., 10.], [10., -45.]]
        why, out, moved = self.choose(xy, (), STREET)
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.OFFROAD, hklamps.OFFROAD, hklamps.OFFROAD])
        np.testing.assert_array_equal(out, np.array(xy), "the others stay where they are")
        why, _, _ = hklamps.choose(np.array(xy, np.float64), [], hklamps.Net([]))
        self.assertEqual(why.tolist(), [hklamps.OFFROAD] * 4, "roads read and none near: nothing is drawn")

    def test_a_bridge_is_a_road_for_the_off_road_rule_not_a_carriageway_to_leave(self):
        deck = ([(0., 0.), (200., 0.)], 5.0, hklamps.K_BRIDGE)
        why, out, moved = self.choose([[100., 30.], [100., 2.]], (), deck)
        self.assertEqual(why.tolist(), [hklamps.KEPT, hklamps.BRIDGE])
        self.assertFalse(moved.any())

    def test_roads_not_read_judge_nothing(self):
        xy = np.array([[0., 0.], [500., 500.]])
        why, out, moved = hklamps.choose(xy, [], hklamps.NO_NET)
        self.assertEqual((why.tolist(), moved.tolist()), ([0, 0], [False, False]))
        np.testing.assert_array_equal(out, xy)

    def test_the_answer_does_not_depend_on_the_order_of_the_points_or_the_blocks(self):
        rng = np.random.RandomState(3)
        xy = np.column_stack([rng.uniform(-50, 250, 4000), rng.uniform(-60, 60, 4000)])
        keep = []
        for k in range(len(xy)):                # (20 m apart: the 6 m rule, which is the order's, cannot meet)
            if all(np.sum(hklamps.dnp.sq(xy[k] - xy[j])) >= 400. for j in keep):
                keep.append(k)
        xy = xy[keep]
        self.assertGreater(len(xy), 40)
        pieces = [STREET, ([(30., -100.), (30., 100.)], 3.0, hklamps.K_GROUND), ([(0., 40.), (200., 40.)], 5.0, hklamps.K_BRIDGE)]
        a = hklamps.choose(xy, [], net_of(*pieces))
        order = rng.permutation(len(xy))
        b = hklamps.choose(xy[order], [], net_of(*pieces))
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x[order], y)
        with mock.patch.object(hklamps, "CELL_M", 17.0):
            c = hklamps.choose(xy, [], net_of(*pieces))
        for x, y in zip(a, c):
            np.testing.assert_array_equal(x, y)


class OneEachTest(unittest.TestCase):
    def test_points_within_a_metre_are_one_lamp_the_first_stays(self):
        xy = np.array([[0., 0.], [0.9, 0.], [1.7, 0.], [20., 0.], [20.5, 0.5], [30., 0.]])
        self.assertEqual(hklamps.one_each(xy).tolist(), [True, False, True, True, False, True],
                         "a chain keeps its first, then what stands clear of it")
        self.assertEqual(hklamps.one_each(np.zeros((0, 2))).tolist(), [])

    def test_a_cell_keeps_one_of_two_posts_within_a_metre_the_lowest_number(self):
        f = [{"attributes": {"OBJECTID": 1, "LAMP_NO": "B2"}, "geometry": {"x": 114.17050, "y": 22.30}},
             {"attributes": {"OBJECTID": 2, "LAMP_NO": "A1"}, "geometry": {"x": 114.170504, "y": 22.30}},      # (0.4 m east)
             {"attributes": {"OBJECTID": 3, "LAMP_NO": "C3"}, "geometry": {"x": 114.1710, "y": 22.30}}]
        got = hklamps.parse(f, (114.17, 22.29, 114.18, 22.31))
        self.assertEqual(sorted(got["no"]), ["A1", "C3"])

    def test_the_cells_of_a_tile_agree_across_their_border_whatever_the_order(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        west = mapcells.cells_for_bbox(22.30, 114.17, 22.30, 114.17)[0]
        east = mapcells.CellKey(west.z, west.x + 1, west.y)
        w, s, e, n = west.bounds()
        pts = [(e - 2e-6, s + 0.002, "B2"), (e + 2e-6, s + 0.002, "A1"), (w + 0.002, s + 0.002, "C3")]      # (the first two 0.4 m apart)
        src = hklamps.Source(d.name, Layer(pts))
        md = MD({}, src)
        for k in (west, east):
            self.assertIsNotNone(src.load(k))
        once = hklamps.points(md, [west, east])
        again = hklamps.points(MD({}, hklamps.Source(d.name, Layer(pts))), [east, west])
        self.assertEqual(sorted(once[2]), ["A1", "C3"], "the lowest number of the pair, the other cell's")
        for x, y in zip(once, again):
            np.testing.assert_array_equal(x, y)
        self.assertEqual(sorted(hklamps.points(md, [west])[2]), ["B2", "C3"], "(a tile that reads one cell only has its own)")


class TileTest(unittest.TestCase):
    """worldprops.tile_props with the lamps asked for."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        # a ground street along y = 100, a flyover along y = 200 (the map's flag); tile (0, 0) is 0..256, its lamps' margin 40
        self.roads = roads_layer([[(-100., 100.), (400., 100.)], [(-100., 200.), (400., 200.)]],
                                 ["residential", "primary"], flags=[(1, "is_bridge")])
        self.gov = points_at(HK, [(50., 105.), (50.4, 105.2), (60., 205.), (150., 108.), (160., 109.), (200., 95.),
                                  (270., 104.), (600., 104.)],
                             ["AA0001", "AA0002", "AA0003", "AA0004", "AA0005", "AA0006", "AA0007", "AA0008"])

    def lamps(self, osm=(), source="layer", frame=HK, spec_args=None):
        """(the lamp rows, the records handed to _aim_lamps, the Source) of tile (0, 0)."""
        layers = {"roads": self.roads, "infrastructure": lamp_layer(osm) if osm else None}
        src = {"layer": lambda: hklamps.Source(self.dir, Layer(self.gov)), "down": lambda: hklamps.Source(self.dir, Down()),
               "disk": lambda: hklamps.Source(self.dir, None), "none": lambda: None}[source]()
        out = []
        with mock.patch.object(wp, "_aim_lamps", wraps=wp._aim_lamps) as aim:
            wp.tile_props(frame, 0, 0, MD(layers, src), elev, lamps=out, spec_args=spec_args)
        return out, (aim.call_args[0][0] if aim.called else []), src

    def test_the_government_points_are_mapped_lamps_with_the_rules_of_choose(self):
        # (AA0002 is one lamp with AA0001; AA0003 stands on the flyover; OpenStreetMap's lamp 2.5 m from AA0004 wins it;
        # AA0007 is 14 m past the tile, a lamp of its margin (LAMP_NEAR_M); AA0008 is 330 m past it)
        out, recs, _ = self.lamps(osm=[(152.5, 108.)])
        got = {r["lamp_id"]: r for r in recs}
        self.assertEqual(sorted(got), ["hk:hyd:AA0001", "hk:hyd:AA0005", "hk:hyd:AA0006", "hk:hyd:AA0007", "osm:node:900"])
        self.assertEqual(sorted((round(r[1]), round(r[2])) for r in out),
                         [(50, 105), (152, 108), (160, 109), (200, 95), (270, 104)], "(x, y) of the rows handed to place")
        one = got["hk:hyd:AA0001"]
        self.assertEqual((one["group"], one["h"], one["arm"], one["lift"], one["rank"]), ("streetlight", wp.STREET_LAMP_H, 0, 0.0, 0.0))
        self.assertTrue(np.isnan(one["layer"]))
        self.assertAlmostEqual(abs((one["yaw"] - got["hk:hyd:AA0006"]["yaw"] + 180.0) % 360.0 - 180.0), 180.0, 3,
                               "the arms turn to the street from either side of it")
        self.assertEqual([r[0] for r in out], ["streetlight"] * 5)

    def test_the_same_lamps_whatever_the_order_the_cells_came_in(self):
        a = self.lamps()[0]
        self.gov = self.gov[::-1]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.assertEqual(self.lamps()[0], a)

    def test_off_the_tile_has_the_openstreetmap_lamps_alone(self):
        osm = [(152.5, 108.)]
        out, recs, _ = self.lamps(osm=osm, source="none")
        self.assertEqual([r["lamp_id"] for r in recs], ["osm:node:900"])
        with mock.patch.object(hklamps, "ON", False):
            again, _, src = self.lamps(osm=osm)
        self.assertEqual(again, out)
        self.assertEqual(src.fetcher.urls, [])
        self.assertEqual(self.lamps(source="none")[0], [], "no lamps at all: none")

    def test_a_place_outside_hong_kong_never_asks(self):
        net = Down()
        src = hklamps.Source(self.dir, net)
        out = []
        wp.tile_props(PRAGUE, 0, 0, MD({"roads": None}, src), elev, lamps=out)
        self.assertEqual((out, net.urls), ([], []))

    def test_records_not_here_yet_are_a_revisit_fault_and_the_next_visit_has_them(self):
        build = revisit.begin()
        try:
            out, recs, _ = self.lamps(osm=[(152.5, 108.)], source="down")
        finally:
            revisit.end(build)
        self.assertGreaterEqual(build.n, 1, "the tile is not kept")
        self.assertEqual([r["lamp_id"] for r in recs], ["osm:node:900"], "built with what the map has")
        build = revisit.begin()
        try:
            full = self.lamps(osm=[(152.5, 108.)])[0]
        finally:
            revisit.end(build)
        self.assertEqual(build.n, 0, "complete: kept")
        self.assertGreater(len(full), len(out))
        build = revisit.begin()
        try:
            again, _, src = self.lamps(osm=[(152.5, 108.)], source="disk")      # (a new process: from disk)
        finally:
            revisit.end(build)
        self.assertEqual((build.n, again), (0, full))

    def test_a_post_in_the_carriageway_stands_beside_it_at_the_width_the_road_builder_gives(self):
        import furniture
        import roads
        self.gov = points_at(HK, [(50., 100.), (60., 104.8), (150., 100.)], ["AA0001", "AA0002", "AA0003"])
        rural = roads.road_specs(self.roads)[0].width
        everywhere = lambda la, lo: np.ones(np.shape(la), bool)         # noqa: E731 (a built-up land cover)
        town = roads.road_specs(self.roads, everywhere, None, False)[0].width
        self.assertNotEqual(rural, town, "the land cover changes the width: the lamps must ask the builder's")
        for args, width in ((None, rural), ((everywhere, None, False), town)):
            d = tempfile.TemporaryDirectory()
            self.addCleanup(d.cleanup)
            self.dir = d.name
            out, recs, _ = self.lamps(spec_args=args)
            got = {r["lamp_id"]: r for r in recs}
            self.assertEqual(sorted(got), ["hk:hyd:AA0001", "hk:hyd:AA0002", "hk:hyd:AA0003"])
            edge = 100. + width / 2 + furniture.LAMP_EDGE_M
            self.assertAlmostEqual(got["hk:hyd:AA0001"]["y"], edge, 2, f"width {width}: out of the carriageway, to the north")
            self.assertAlmostEqual(got["hk:hyd:AA0001"]["x"], 50., 2, "straight out: along the perpendicular")
            _bx, by, _bz = HK.geodetic_to_enu(got["hk:hyd:AA0001"]["lat"], got["hk:hyd:AA0001"]["lon"], 0.0)
            self.assertAlmostEqual(float(by), got["hk:hyd:AA0001"]["y"], 2, "its lat / lon follow it")

    def test_a_post_clear_of_the_carriageway_stays_where_the_department_has_it(self):
        self.gov = points_at(HK, [(50., 106.), (150., 94.)], ["AA0001", "AA0002"])      # (7 rural half width 3.5: both beyond)
        out, recs, _ = self.lamps()
        self.assertEqual(sorted((round(r["x"], 2), round(r["y"], 2)) for r in recs), [(50., 106.), (150., 94.)])

    def test_a_post_with_no_road_within_40_m_is_not_a_lamp(self):
        self.gov = points_at(HK, [(50., 105.), (50., 150.), (100., 20.)], ["AA0001", "AA0002", "AA0003"])
        out, recs, _ = self.lamps()
        self.assertEqual([r["lamp_id"] for r in recs], ["hk:hyd:AA0001"])

    def test_a_tile_without_a_map_cell_of_roads_keeps_the_points_it_cannot_judge(self):
        self.roads = None
        out, recs, _ = self.lamps()
        self.assertEqual(sorted(r["lamp_id"] for r in recs),
                         ["hk:hyd:AA0001", "hk:hyd:AA0003", "hk:hyd:AA0004", "hk:hyd:AA0005", "hk:hyd:AA0006",
                          "hk:hyd:AA0007"])
        self.assertEqual(sorted((round(r["x"]), round(r["y"])) for r in recs),
                         [(50, 105), (60, 205), (150, 108), (160, 109), (200, 95), (270, 104)], "where the department has them")


if __name__ == "__main__":
    unittest.main()
