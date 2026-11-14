"""railstations.py (task 037): platforms on the standard edge, roofs, links across the tracks, on items dumped from eight real
stations (tools/dump_railstation_fixture.py)."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import busstations as bs  # noqa: E402
import railstations as rs  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "railstations")
COUNTRY = {"vinnytsia": "UA", "kalynivka": "UA", "kozyatyn": "UA", "kyiv_pas": "UA", "hk_hln": "CZ", "trutnov": "CZ",
           "pardubice": "CZ", "passau": "DE"}
_CACHE = {}


def items_of(name):
    if name not in _CACHE:
        with open(os.path.join(DATA, name + ".json"), encoding="utf-8") as f:
            _CACHE[name] = bs.items_from_json(json.load(f)["items"])
    return _CACHE[name]


_STATIONS = {}


def stations_of(name, items=None):
    if items is not None:
        return rs.stations(items, COUNTRY[name])
    if name not in _STATIONS:
        _STATIONS[name] = rs.stations(items_of(name), COUNTRY[name])
    return _STATIONS[name]


def signature(sts):
    return [(st.kind, st.confidence, [(p.id, np.round(p.ring, 2).tolist()) for p in st.platforms],
             [(l.kind, l.ids, np.round(l.xy, 2).tolist()) for l in st.links]) for st in sts]


def dist(xy, p):
    return bs.dist_to_path(xy, p)


class RailStationsTest(unittest.TestCase):
    def test_every_pilot_station_is_found(self):
        for name in COUNTRY:
            sts = stations_of(name)
            self.assertEqual(len(sts), 1, name)
            self.assertGreaterEqual(len(sts[0].platforms), 2, name)
            self.assertGreaterEqual(sts[0].confidence, rs.MIN_CONFIDENCE, name)

    def test_kyiv_three_seeds_are_one_station(self):
        st = stations_of("kyiv_pas")[0]
        self.assertGreaterEqual(len(st.platforms), 7)
        self.assertGreaterEqual(len(st.roofs), 1)

    def test_tram_and_bus_platforms_are_not_ours(self):
        tram = {"layer": "infrastructure", "cls": "platform", "kind": "polygon", "tags": {"railway": "platform", "tram": "yes"}}
        bus = {"layer": "infrastructure", "cls": "platform", "kind": "line", "tags": {"highway": "platform", "bus": "yes"}}
        rail = {"layer": "infrastructure", "cls": "platform", "kind": "line", "tags": {"railway": "platform", "train": "yes"}}
        self.assertFalse(rs.is_train_platform(tram))
        self.assertFalse(rs.is_train_platform(bus))
        self.assertTrue(rs.is_train_platform(rail))

    def test_snapped_points_stand_on_the_standard_edge(self):
        for name in ("vinnytsia", "kozyatyn", "kyiv_pas", "hk_hln", "passau"):
            edge = rs.norm(COUNTRY[name])[0]
            st = stations_of(name)[0]
            checked = 0
            for pl in st.platforms:
                for i in np.flatnonzero(pl.snapped)[::7]:
                    track = next(xy for tid, xy in st.tracks if True) if False else None
                    near = min(dist(xy, pl.ring[i]) for _tid, xy in st.tracks)
                    self.assertAlmostEqual(near, edge, delta=0.06, msg=f"{name} {pl.id}")
                    checked += 1
            self.assertGreater(checked, 20, name)

    def test_links_across_the_tracks(self):
        kinds = {n: sorted(l.kind for l in stations_of(n)[0].links) for n in COUNTRY}
        # Vinnytsia: an underpass with stairs at both ends; Kozyatyn: a footbridge with stairs
        vin = [l for l in stations_of("vinnytsia")[0].links if l.kind == "under"]
        self.assertTrue(vin and len(vin[0].steps) >= 2)
        koz = [l for l in stations_of("kozyatyn")[0].links if l.kind == "over"]
        self.assertTrue(koz and len(koz[0].steps) >= 1)
        kyiv = stations_of("kyiv_pas")[0].links
        self.assertGreaterEqual(sum(l.kind == "under" for l in kyiv), 2)
        self.assertGreaterEqual(sum(l.kind == "over" for l in kyiv), 1)
        self.assertIn("over", kinds["passau"])
        self.assertIn("over", kinds["pardubice"])
        self.assertIn("under", kinds["pardubice"])
        for n in COUNTRY:
            for l in stations_of(n)[0].links:
                self.assertTrue(l.crosses, n)           # (every link crosses a track)

    def test_road_bridge_pavement_is_no_link(self):
        items = [dict(i) for i in items_of("kyiv_pas")]
        before = len(stations_of("kyiv_pas", items)[0].links)
        walk = {"layer": "roads", "id": "zz", "cls": "footway", "sub": "road", "name": "", "tags": {}, "kind": "line",
                "level": 1.0, "subclass": "sidewalk", "xy": np.array([[-30.0, 20.0], [30.0, 20.0]])}
        self.assertEqual(len(stations_of("kyiv_pas", items + [walk])[0].links), before)

    def test_deterministic(self):
        for name in ("vinnytsia", "kyiv_pas"):
            self.assertEqual(signature(stations_of(name)), signature(stations_of(name)))
            rev = list(reversed(items_of(name)))                        # (the order of the items does not matter)
            self.assertEqual(signature(stations_of(name)), signature(stations_of(name, rev)))

    def test_no_track_no_station(self):
        items = [i for i in items_of("kalynivka") if not (i["layer"] == "roads" and i["sub"] == "rail")]
        self.assertEqual(stations_of("kalynivka", items), [])

    def test_a_station_with_no_platform_gets_one_along_its_track(self):
        items = [i for i in items_of("kalynivka") if i["cls"] != "platform"]
        sts = stations_of("kalynivka", items)
        self.assertEqual(len(sts), 1)
        st = sts[0]
        self.assertTrue(st.synth)
        self.assertEqual(len(st.platforms), 1)
        pl = st.platforms[0]
        self.assertFalse(pl.mapped)
        self.assertGreater(bs.area(pl.ring), 300.0)
        edge = rs.norm("UA")[0]
        near = [min(dist(xy, pl.ring[i]) for _tid, xy in st.tracks) for i in np.flatnonzero(pl.snapped)]
        self.assertTrue(near)
        for d in near:
            self.assertAlmostEqual(d, edge, delta=0.1)

    def test_norms(self):
        self.assertAlmostEqual(rs.norm("UA", 5)[1], 1.10)
        self.assertAlmostEqual(rs.norm("UA", 1)[1], 0.20)
        self.assertAlmostEqual(rs.norm("CZ", 5)[0], 1.67)
        self.assertEqual(rs.norm(None, 5), (rs.EDGE_M["default"], rs.HEIGHT_M["default"]["high"]))

    def test_height_tag(self):
        pl = rs.Platform("x", np.zeros((3, 2)), {"height": "0.55"})
        self.assertAlmostEqual(pl.height_tag, 0.55)
        self.assertIsNone(rs.Platform("x", np.zeros((3, 2)), {"height": "7"}).height_tag)    # (not metres of a platform)
        self.assertIsNone(rs.Platform("x", np.zeros((3, 2)), {}).height_tag)


class SnapTest(unittest.TestCase):
    def test_a_platform_mapped_to_the_axis_ends_on_the_edge(self):
        track = [("t", np.array([[0.0, 0.0], [100.0, 0.0]]))]
        ring = np.array([[10.0, 0.5], [90.0, 0.5], [90.0, 5.0], [10.0, 5.0]])          # (the edge runs 0.5 m off the axis)
        out, snapped, tidx = rs.snap_edges(ring, track, 1.7)
        self.assertTrue(snapped.any())
        np.testing.assert_allclose(out[snapped][:, 1], 1.7, atol=1e-6)
        self.assertTrue((out[~snapped][:, 1] >= 4.9).all())                            # (the back is as mapped)

    def test_the_far_side_stays(self):
        track = [("t", np.array([[0.0, 0.0], [100.0, 0.0]]))]
        ring = np.array([[10.0, 20.0], [90.0, 20.0], [90.0, 25.0], [10.0, 25.0]])      # (20 m away: another track's)
        out, snapped, _ = rs.snap_edges(ring, track, 1.7)
        self.assertFalse(snapped.any())

    def test_an_edge_beside_a_curve_follows_it(self):
        t = np.linspace(0.0, np.pi / 2, 60)
        axis = np.c_[200.0 * np.sin(t), 200.0 * (1 - np.cos(t))]                      # an arc of 200 m
        n = np.c_[-np.cos(t) * 0 + np.sin(np.pi / 2 - t) * 0, np.ones_like(t)]
        track = [("t", axis)]
        inner = axis + np.c_[-np.sin(t + np.pi / 2 - np.pi / 2) * 0, np.zeros_like(t)]
        # a platform ring 3 m to the left of the arc (its centre side)
        left = np.c_[-np.sin(np.pi / 2 - t) * 0 - np.cos(t) * 0, np.zeros_like(t)]
        tx, ty = np.cos(t), np.sin(t)
        ring = np.vstack((axis + np.c_[-ty, tx] * 3.0, (axis + np.c_[-ty, tx] * 8.0)[::-1]))
        out, snapped, tidx = rs.snap_edges(ring, track, 1.7)
        pts = out[snapped]
        self.assertGreater(len(pts), 20)
        d = np.array([bs.dist_to_path(axis, p) for p in pts])
        np.testing.assert_allclose(d, 1.7, atol=0.05)


if __name__ == "__main__":
    unittest.main()
