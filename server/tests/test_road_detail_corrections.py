"""Road detail regressions using generated assets and controlled junctions."""

import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "tools")]
import build_europe_guardrails as rails
import build_europe_stops as stops
import euguardrails
import frgore
import frroadmesh
import frstreets
import furniture
import geodesy
import poleprofiles
import regionalstops
import roads
import signmesh
import signplacement as placement

FRAME = geodesy.EnuFrame(48.6, 7.7)
BOX = (0.0, 0.0, 256.0, 256.0)
FR = lambda *p: "FR"


def road(fid, points, cls="residential", width=6.0, oneway=False, link=False, reverse=False):
    spec = roads.Spec(
        width, "paved", cls != "motorway", cls, oneway, dir=1 if oneway else 0, link=link
    )
    p = np.c_[np.asarray(points, float), np.zeros(len(points))]
    return fid, spec, p[::-1] if reverse else p, 7.7


def rect(x0, y0, x1, y1):
    return np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], float)


class PlacementTest(unittest.TestCase):
    def test_line_of_sight_and_full_width_detect_separate_obstruction(self):
        context = placement.Context([rect(4.0, -1.0, 6.0, 1.0)])
        self.assertTrue(context.clear((9.0, 0.0)))
        self.assertFalse(context.post((9.0, 0.0), (1.0, 0.0), 0.8, [(0.0, 0.0)]))
        self.assertTrue(context.post((9.0, 0.0), (1.0, 0.0), 0.8, [(7.0, 0.0)]))
        # A narrow projection can intersect the panel even between sample points.
        sliver = placement.Context([rect(8.99, 0.19, 9.01, 0.22)])
        self.assertFalse(sliver.post((9.0, 0.0), (1.0, 0.0), 0.8, []))

    def street_rows(self, rr, obstacles):
        m = signmesh.Mesh()
        with patch("roadphones.nearby", return_value=[]):
            posts, records = frstreets.place(
                FRAME,
                rr,
                BOX,
                FR,
                m,
                {"a": "Rue du Houblon", "b": "Rue de l’Église"},
                obstacles=obstacles,
            )
        return m, posts, records

    def test_wall_and_post_both_survive_with_clearance_and_no_duplicate_pole(self):
        rr = [road("a", [(100, 100), (200, 100)]), road("b", [(100, 100), (100, 200)])]
        ring = rect(105, 105, 120, 118)
        import buildings

        spec = buildings.Spec(ring=ring, roof="flat", height=6.2, floor_h=3.1, seed="house")
        with patch.object(buildings, "near_rings", return_value=[ring]) as nearby:
            obs = placement.for_tile(FRAME, 0, 0, None, [(spec, 0.0, 0.0)])
            nearby.assert_not_called()
            mesh, posts, records = self.street_rows(rr, obs)
            nearby.assert_called_once()
        self.assertEqual({r["mount"] for r in records}, {"wall", "post"})
        wall = next(r for r in records if r["mount"] == "wall")
        self.assertAlmostEqual(wall["pos"][1], 104.966)
        self.assertGreater(wall["pos"][2], 2.4)
        self.assertEqual([r[0] for r in posts], ["fr_street_post"])
        self.assertTrue(obs.clear(posts[0][1:3]))
        # The actual plaque remains on the exterior, including its metal back.
        near_wall = np.asarray(mesh.p)
        near_wall = near_wall[near_wall[:, 0] > 102]
        self.assertGreater(len(near_wall), 10)
        self.assertLess(near_wall[:, 1].max(), 105.0)

    def test_street_search_does_not_spawn_on_road_or_in_houses(self):
        rr = [
            road("a", [(100, 100), (109, 100), (132, 104), (170, 111)]),
            road("b", [(100, 100), (100, 170)]),
        ]
        obs = placement.Context([rect(105, 103.8, 123, 113), rect(106, 94, 116, 96.7)])
        _, posts, records = self.street_rows(rr, obs)
        self.assertTrue(posts)
        paved = placement.pavement([(r[1].width / 2, r[2]) for r in rr])
        self.assertTrue(all(paved.clear(p[1:3], 0.29) and obs.clear(p[1:3]) for p in posts))
        self.assertTrue(all(r["mount"] == "post" for r in records))

    def test_stop_moves_outside_building_without_switching_approach(self):
        rr = [
            road("west", [(50, 100), (100, 100)], "secondary"),
            road("east", [(100, 100), (150, 100)], "secondary"),
            road("north", [(100, 100), (100, 160)]),
        ]
        old = furniture.junction_props(rr, BOX, signals=[])
        self.assertEqual(len(old), 1)
        x, y = old[0][1:3]
        obs = placement.Context(
            [rect(x - 0.9, y - 1.5, x + 0.8, y + 1.5)], ground=lambda x, y: np.full_like(x, 5.0)
        )
        self.assertFalse(obs.clear((x, y)))
        new = furniture.junction_props(rr, BOX, signals=[], sign_obstacles=obs)
        self.assertEqual(len(new), 1)
        row = new[0]
        self.assertEqual(row[0], "stop")
        self.assertLess(row[1], 97.0)
        self.assertAlmostEqual(row[3], 5.0)
        self.assertEqual(row[4], old[0][4])
        self.assertTrue(obs.post(row[1:3], (0, 1), 0.94, [(100, row[2] + 12), (100, row[2] + 25)]))


class GoreTest(unittest.TestCase):
    def fork(self, ramp_points=((20, 0), (95, -26), (140, -65)), reverse=False):
        return [
            road("through", [(-90, 0), (20, 0)], "motorway", 7.0, True),
            road("through", [(20, 0), (180, 0)], "motorway", 7.0, True),
            road("exit", ramp_points, "motorway", 4.0, True, True, reverse),
        ]

    def test_index_free_diverge_and_owned_once_across_tile_edge(self):
        rr = self.fork()
        ground = lambda x, y: np.full_like(x, 3.75)
        (row,) = frgore.place(FRAME, rr, (0, -256, 50, 256), FR, ground)
        self.assertEqual(row[0], "fr_j14a")
        self.assertEqual(row[3], 3.75)
        self.assertGreater(row[1], 50.0)
        self.assertLess(row[2], -5.0)
        self.assertEqual(row[4], -90.0)
        self.assertEqual(frgore.place(FRAME, rr, (50, -256, 256, 256), FR, ground), [])

    def test_small_model_only_when_gap_allows_and_no_merges(self):
        b = (0, -256, 256, 256)
        small = self.fork(((20, 0), (95, -10), (140, -10)))
        (row,) = frgore.place(FRAME, small, b, FR)
        self.assertEqual(row[0], "fr_j14a_small")
        too_narrow = self.fork(((20, 0), (95, -8), (140, -8)))
        self.assertEqual(frgore.place(FRAME, too_narrow, b, FR), [])
        self.assertEqual(frgore.place(FRAME, self.fork(reverse=True), b, FR), [])
        self.assertEqual(frgore.place(FRAME, self.fork(), b, lambda *p: "DE"), [])

    def test_connected_vertex_inside_an_unsplit_mainline(self):
        main = road("main", [(-90, 0), (20, 0), (180, 0)], "motorway", 7.0, True)
        ramp = self.fork()[-1]
        self.assertEqual(len(frgore.place(FRAME, [main, ramp], (0, -256, 256, 256), FR)), 1)
        elevated = (*main[:2], main[2] + [0.0, 0.0, 5.0], main[3])
        self.assertEqual(frgore.place(FRAME, [elevated, ramp], (0, -256, 256, 256), FR), [])


class ModelTest(unittest.TestCase):
    def test_reflectors_fit_between_corrugation_crests(self):
        for family, lo, hi, front in [
            ("a", 0.58, 0.65, -0.075),
            ("b", 0.55, 0.66, -0.075),
            ("thin", 0.56, 0.65, -0.045),
        ]:
            model = rails.module(family, "white_red")
            for colour in ("white", "red"):
                p = np.asarray(model.p)[np.asarray(model.tri["bne_eur_" + colour])]
                self.assertGreater(p[:, :, 2].min(), lo)
                self.assertLess(p[:, :, 2].max(), hi)
                self.assertGreater(p[:, :, 1].min(), front)
                # The near face must also be visible past the steel spacer.
                point = p.reshape(-1, 3).mean(0)[[0, 2]]
                hits = []
                for name, triangles in model.tri.items():
                    for a, b, c in np.asarray(model.p)[triangles]:
                        basis = np.column_stack((b - a, c - a))[[0, 2]]
                        if abs(np.linalg.det(basis)) < 1e-10:
                            continue
                        u, v = np.linalg.solve(basis, point - a[[0, 2]])
                        if min(u, v, 1 - u - v) >= -1e-7:
                            hits.append((a[1] + u * (b[1] - a[1]) + v * (c[1] - a[1]), name))
                self.assertEqual(min(hits)[1], "bne_eur_" + colour)

    def test_street_pole_and_j14_have_no_collision_geometry(self):
        for n in ("fr_street_post", "fr_j14a", "fr_j14a_small"):
            tree = ET.parse(rails.OUT / ("bne_" + n + ".dae"))
            ids = {e.get("id", "") for e in tree.iter()}
            self.assertFalse(any(i.startswith("Colmesh") or "collision-1" in i for i in ids))
            self.assertIn(n + "40-g", ids)
            self.assertIn(n + "4-g", ids)
        for small in (False, True):
            m = rails.j14(small)
            p = np.asarray(m.p)
            t = p[np.asarray([t for v in m.tri.values() for t in v])]
            self.assertGreater(
                np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1).min(), 1e-6
            )
            self.assertAlmostEqual(float(np.ptp(p[:, 0])), 1.0 if small else 2.0)

    def test_stop_assets_match_all_enabled_territories_and_preserve_source_art(self):
        modelmap = regionalstops.models()
        self.assertEqual(set(modelmap), set(euguardrails.PROFILES))
        source = json.loads((stops.ART / "sources.json").read_text(encoding="utf-8"))
        materials = json.loads((stops.OUT / "main.materials.json").read_text())
        for code, face in source["faces"].items():
            n = "bne_eu_stop_" + code
            self.assertEqual(
                hashlib.sha256((stops.ART / (code + ".svg")).read_bytes()).hexdigest(),
                face["sha256"],
            )
            self.assertTrue((stops.OUT / (n + ".dae")).exists())
            self.assertTrue((stops.OUT / (n + ".dds")).exists())     # (shipped as DDS: tools/mod_textures_dds.py)
            self.assertTrue(
                materials[n + "_face"]["Stages"][0]["baseColorMap"].endswith("/" + n + ".dds")
            )
        self.assertNotEqual(
            (stops.OUT / "bne_eu_stop_fr.dds").read_bytes(),
            (stops.OUT / "bne_eu_stop_de.dds").read_bytes(),
        )
        self.assertNotEqual(
            (stops.OUT / "bne_eu_stop_fr.dds").read_bytes(),
            (stops.OUT / "bne_eu_stop_gb.dds").read_bytes(),
        )

    def test_stop_localization_preserves_pose_and_legacy_quebec(self):
        raw = [["stop", 1.0, 2.0, 3.0, 90.0, 1.0, 3.0, 0.0]]
        with patch.dict(os.environ, {"BNE_EU_ROAD_DETAILS": "on"}):
            profile = poleprofiles.geographic_profile("europe")
            (fr,) = regionalstops.localize(raw, profile, FRAME, FR)
            self.assertEqual(fr[0], "eu_stop_fr")
            self.assertEqual(fr[1:6], raw[0][1:6])
            self.assertEqual(fr[6], stops.height(0.80))
            self.assertEqual(regionalstops.localize(raw, profile, FRAME, lambda *p: "US"), raw)
            qc = geodesy.EnuFrame(45.50, -73.56)
            self.assertEqual(
                regionalstops.localize(raw, profile, qc, lambda *p: "CA")[0][0], "stop_arret"
            )
        with patch.dict(os.environ, {"BNE_EU_ROAD_DETAILS": "off"}):
            profile = poleprofiles.geographic_profile("europe")
            self.assertEqual(regionalstops.localize(raw, profile, FRAME, FR), raw)


if __name__ == "__main__":
    unittest.main()
