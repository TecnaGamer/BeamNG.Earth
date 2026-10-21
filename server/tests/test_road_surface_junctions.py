"""Lower-priority ground surfaces stop at paved junctions without painting over their lanes."""
import json
import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roadmesh
import roads
import geodesy
import mapgeom
from test_roads import features, rules, FRAME, flat


class UnpavedJunction(unittest.TestCase):
    def test_cached_barbing_tracks_do_not_cover_a3_lanes(self):
        record = json.loads((Path(__file__).parent / "fixtures/a3_barbing_track_junctions.json").read_text())
        items = record["roads"]
        n = len(items)
        frame = geodesy.EnuFrame(*record["anchor"])
        f = mapgeom.Features(layer="roads", ids=np.array([r["id"] for r in items]),
                             kind=np.full(n, mapgeom.LINE, np.uint8), part_off=np.arange(n + 1),
                             ring_off=np.arange(n + 1), coord_off=np.r_[0, np.cumsum([len(r["lonlat"]) for r in items])],
                             lonlat=np.concatenate([r["lonlat"] for r in items]),
                             attrs={"class": np.array([r["class"] for r in items]), "subtype": np.full(n, "road")},
                             rules={"width": rules([(i, r["width"]) for i, r in enumerate(items)], "float"),
                                    "surface": rules([(i, "unpaved" if r["kind"] == "dirt" else "paved") for i, r in enumerate(items)])})
        chunks = [c for tx in (-1, 0) for ty in (-1, 0)
                  for c in roads.build_tile(frame, tx, ty, [f], flat)]
        dirt = [c for c in chunks if c["m"] == roads.LOOK["dirt"]["m"]]
        self.assertGreaterEqual(len(dirt), 2, "both real farm-track approaches stay visible")
        edges = np.concatenate([np.r_[left, right] for left, right, *_rest in roadmesh.ribbons(dirt)])
        paved = [np.c_[roads.mapgeom.enu_xy(f, frame, f.parts(i)[0][0])]
                 for i, r in enumerate(items) if r["kind"] == "paved"]
        distance = np.min([roads._seg_dist(edges, p[:-1], p[1:]).min(1) for p in paved], axis=0)
        self.assertGreaterEqual(float(distance.min()), 6.495, "no dirt triangles enter either A3 carriageway")

    def check_clear(self, chunks, line, half):
        dirt = [c for c in chunks if c["m"] in {roads.LOOK["dirt"]["m"], roads.LOOK["gravel"]["m"]}]
        self.assertTrue(dirt, "the track approach outside the paved carriageway stays visible")
        edges = np.concatenate([np.r_[left, right] for left, right, *_rest in roadmesh.ribbons(dirt)])
        distance = roads._seg_dist(edges, line[:-1], line[1:]).min(1)
        self.assertGreaterEqual(float(distance.min()), half - 0.005, "no unpaved ribbon covers a paved lane")
        self.assertLess(float(distance.min()), half + 0.4, "the approach reaches the paved edge")

    def test_dirt_and_gravel_join_the_pavement_at_its_edge(self):
        for surface in ("unpaved", "gravel"):
            with self.subTest(surface=surface):
                f = features([[(-200, 40), (300, 40)], [(100, -100), (100, 40)]],
                             ["motorway", "track"], widths=[(0, 13.0)], surfaces=[(1, surface)])
                ring = roads.ground_parts(f, 0)[0]
                paved = np.c_[roads.mapgeom.enu_xy(f, FRAME, ring)]
                chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
                self.check_clear(chunks, paved, 6.5)

    def test_angled_track_crossing_keeps_both_approaches(self):
        f = features([[(-200, 80), (300, 80)], [(40, -30), (180, 180)]],
                     ["motorway", "track"], widths=[(0, 13.0)])
        ring = roads.ground_parts(f, 0)[0]
        paved = np.c_[roads.mapgeom.enu_xy(f, FRAME, ring)]
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
        self.check_clear(chunks, paved, 6.5)
        dirt = [c for c in chunks if c["m"] == roads.LOOK["dirt"]["m"]]
        self.assertEqual(len(dirt), 2)

    def test_pavement_in_neighboring_tile_also_clips_the_track(self):
        f = features([[(262, -100), (262, 200)], [(200, 64), (262, 64)]],
                     ["motorway", "track"], widths=[(0, 13.0)])
        ring = roads.ground_parts(f, 0)[0]
        paved = np.c_[roads.mapgeom.enu_xy(f, FRAME, ring)]
        self.check_clear(roads.build_tile(FRAME, 0, 0, [f], flat), paved, 6.5)

    def test_fixed_bridge_surface_is_not_trimmed_at_a_ground_crossing(self):
        f = features([[(-200, 80), (300, 80)], [(100, 10), (100, 180)]],
                     ["motorway", "track"], widths=[(0, 13.0)], flags=[(1, "is_bridge")])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
        decks = [c for c in chunks if c.get("deck")]
        self.assertTrue(decks)
        self.assertTrue(any(np.min(np.array(c["n"])[:, 1]) < 80 < np.max(np.array(c["n"])[:, 1]) for c in decks))


if __name__ == "__main__":
    unittest.main()
