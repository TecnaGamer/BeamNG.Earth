"""Regional pole POC: geographic opt-in, tile ownership, wires, cache and exported geometry.

Synthetic data only. These tests do NOT validate BeamNG's actual shape importer,
in-game collision, visual LOD transitions or performance.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))
import furniture
import poleprofiles as pp
import roads
import build_regional_poles as mesh


def road(fid="example", urban=False):
    return (fid, roads.Spec(8.0, "paved", urban, "residential", False),
            np.array([[-200., 50., 2.], [200., 50., 4.], [700., 80., 8.]]), 7.79)


def concrete_id():
    return next(str(i) for i in range(100) if pp.choose(pp.FR_DEMO, str(i)) == pp.RECESSED)


def stock_demo_id():
    return next(str(i) for i in range(100) if pp.choose(pp.FR_DEMO, str(i)) == pp.STOCK)


def wire_rows(wires):
    return [(np.asarray(w[0] if isinstance(w, tuple) else w).tolist(), w[1] if isinstance(w, tuple) else None)
            for w in wires]


class SelectionTest(unittest.TestCase):
    def test_fr_all_replaces_every_road_only_in_classified_france(self):
        ids = [str(i) for i in range(200)]
        for country in ("FR", "fr", " FR "):
            profile = pp.profile_for(country, "fr-all")
            self.assertEqual(profile, pp.FR_ALL)
            self.assertTrue(all(pp.choose(profile, fid) == pp.RECESSED for fid in ids))
        for country in ("DE", "BE", "CH", "ES", "IT", "US", "CA", None, "", "ZZ"):
            profile = pp.profile_for(country, "fr-all")
            self.assertIsNone(profile)
            self.assertTrue(all(pp.choose(profile, fid) == pp.STOCK for fid in ids))

    def test_only_opted_in_fr(self):
        self.assertEqual(pp.profile_for("fr", "fr-demo"), pp.FR_DEMO)
        for country in ("FR", "DE", "US", "CA", None, "", "ZZ"):
            self.assertIsNone(pp.profile_for(country))
            if country != "FR":
                self.assertIsNone(pp.profile_for(country, "fr-demo"))
        self.assertIsNone(pp.profile_for("FR", "unknown"))

    def test_stable_mix_and_unknown_profile(self):
        ids = [str(i) for i in range(200)]
        a = {fid: pp.choose(pp.FR_DEMO, fid) for fid in ids}
        b = {fid: pp.choose(pp.FR_DEMO, fid) for fid in reversed(ids)}
        self.assertEqual(a, b)
        self.assertEqual(set(a.values()), {pp.STOCK, pp.RECESSED})
        self.assertTrue(all(pp.choose(None, fid) == pp.STOCK for fid in ids))
        self.assertTrue(all(pp.choose("not-a-profile", fid) == pp.STOCK for fid in ids))

    def test_server_profile_does_not_use_anchor_or_airport_country(self):
        import earthd
        import countrybounds
        with mock.patch.object(earthd.navgraphmod, "country_near", side_effect=AssertionError("airport lookup")), \
                mock.patch.object(countrybounds, "bundled", side_effect=AssertionError("data read by token")):
            for mode in ("off", "fr-demo", "fr-all"):
                server = SimpleNamespace(regional_poles=mode, airports_db=None)
                for lat, lon in ((48.815, 7.79), (48.58, 7.82), (39., -76.)):
                    self.assertEqual(earthd.Server.pole_profile(server, SimpleNamespace(lat=lat, lon=lon)),
                                     pp.geographic_profile(mode))


class PlacementTest(unittest.TestCase):
    def test_fr_all_replaces_a_demo_stock_road_in_both_placement_paths(self):
        for urban, sidewalks in ((False, False), (True, False), (True, True)):
            r = road(stock_demo_id(), urban)
            walks = [{"fid": r[0], "pts": roads.offset_polyline(r[2], side * 5.25), "side": side, "w": 2.5}
                     for side in (-1, 1)] if sidewalks else []
            args = ([r], walks, (0, 0, 512, 256))
            old_wires, new_wires = [], []
            old = furniture.place(*args, town=urban, wires=old_wires, pole_profile=pp.FR_DEMO)
            new = furniture.place(*args, town=urban, wires=new_wires, pole_profile=pp.FR_ALL)
            poles = [r for r in old if r[0] == pp.STOCK.group]
            replacements = [r for r in new if r[0] == pp.RECESSED.group]
            self.assertGreater(len(poles), 5)
            self.assertNotIn(pp.STOCK.group, [r[0] for r in new])
            self.assertEqual([r[1:6] + r[7:] for r in poles], [r[1:6] + r[7:] for r in replacements])
            self.assertEqual([r for r in old if r[0] != pp.STOCK.group],
                             [r for r in new if r[0] != pp.RECESSED.group])
            self.assertEqual(len(old_wires), 3 * len(new_wires))
            self.assertTrue(all(radius == pp.RECESSED.wire_radius for _, radius in new_wires))

    def placed(self, box, urban=False, sidewalks=False, **kw):
        r = road(concrete_id(), urban)
        walks = [{"fid": r[0], "pts": roads.offset_polyline(r[2], side * 5.25), "side": side, "w": 2.5}
                 for side in (-1, 1)] if sidewalks else []
        wires = []
        rows = furniture.place([r], walks, box, wires=wires, town=urban, **kw)
        return rows, wires

    def test_both_placement_paths_and_lamps_unchanged(self):
        for urban, walks in ((False, False), (True, False), (True, True)):
            old, ow = self.placed((0, 0, 512, 256), urban, walks)
            new, nw = self.placed((0, 0, 512, 256), urban, walks, pole_profile=pp.FR_DEMO)
            a = [r for r in old if r[0] == pp.STOCK.group]
            b = [r for r in new if r[0] == pp.RECESSED.group]
            self.assertGreater(len(b), 5)
            self.assertEqual([r[1:6] + r[7:] for r in a], [r[1:6] + r[7:] for r in b])
            self.assertTrue(all(r[6] == pp.RECESSED.height for r in b))
            self.assertEqual([r for r in old if r[0] != pp.STOCK.group],
                             [r for r in new if r[0] != pp.RECESSED.group])
            self.assertEqual(len(ow), 3 * len(nw))
            self.assertTrue(all(isinstance(w, tuple) and w[1] == pp.RECESSED.wire_radius for w in nw))

    def test_buried_lines_still_have_no_poles_or_wires(self):
        rows, wires = self.placed((0, 0, 512, 256), True, pole_profile=pp.FR_DEMO, buried=1.0)
        self.assertNotIn(pp.RECESSED.group, [r[0] for r in rows])
        self.assertTrue(any(r[0] == furniture.G_LAMP for r in rows))
        self.assertEqual(wires, [])

    def test_split_tiles_same_items_and_spans_as_union(self):
        full, fw = self.placed((0, 0, 512, 256), pole_profile=pp.FR_DEMO)
        left, lw = self.placed((0, 0, 256, 256), pole_profile=pp.FR_DEMO)
        right, rw = self.placed((256, 0, 512, 256), pole_profile=pp.FR_DEMO)
        self.assertEqual(sorted(full), sorted(left + right))
        self.assertEqual(wire_rows(fw), wire_rows(lw + rw))
        self.assertEqual(len({tuple(r) for r in full}), len(full))
        again, aw = self.placed((256, 0, 512, 256), pole_profile=pp.FR_DEMO)
        self.assertEqual(right, again)
        self.assertEqual(wire_rows(rw), wire_rows(aw))

    def test_anchors_follow_rotated_poles_and_sag(self):
        poles = [(10., 20., 3., 90.), (40., 60., 5., 180.)]
        (p, radius), = furniture.wire_lines(poles, pp.RECESSED.wire_anchors, pp.RECESSED.wire_radius)
        np.testing.assert_allclose(p[0], [10., 20.25, 10.85], atol=1e-12)
        np.testing.assert_allclose(p[-1], [39.75, 60., 12.85], atol=1e-12)
        self.assertLess(p[len(p) // 2, 2], (p[0, 2] + p[-1, 2]) / 2)
        self.assertEqual(radius, 0.025)

    def test_bundled_wire_under_bridge_and_clear_span(self):
        wires = furniture.wire_lines([(0, 0, 0, 0), (40, 0, 0, 0)],
                                     pp.RECESSED.wire_anchors, pp.RECESSED.wire_radius)
        saved = wire_rows(wires)
        furniture.under_decks([], [(np.array([[0., 0., 15.], [40., 0., 15.]]), 5.)], wires)
        self.assertEqual(wire_rows(wires), saved)
        furniture.under_decks([], [(np.array([[0., 0., 8.], [40., 0., 8.]]), 5.)], wires)
        self.assertEqual(wires, [])

    def test_real_tile_serialization_has_group_and_wire_mesh(self):
        rows, wires = self.placed((0, 0, 256, 256), pole_profile=pp.FR_DEMO)
        with tempfile.TemporaryDirectory() as d:
            roads.write_tile(d, "/temp/test", 1, 0, 0, [], props=rows, wires=wires)
            data = json.loads(Path(d, "roads_1_0_0.json").read_text())
            self.assertIn(pp.RECESSED.group, data["props"]["groups"])
            self.assertEqual(len(data["props"]["items"]), len(rows))
            self.assertTrue(Path(d, Path(data["wire"]["shape"]).name).is_file())
            import cdae
            self.assertIsNotNone(cdae.read(Path(d, "wire_1_0_0.cdae")))


class PipelineTest(unittest.TestCase):
    def test_real_worker_build_switches_models_without_stale_cache(self):
        import blockworker
        import threading
        from test_roads import FRAME, LAT, LON, features
        from test_blockworker import FakeMapData, FakeDem
        keys = roads.cells_of_tile(FRAME, 0, 0)
        for fid in (concrete_id(), stock_demo_id()):
            f = features([[(-300., 100.), (600., 100.)]], ["residential"])
            f.ids[:] = [fid]
            with self.subTest(road_id=fid), tempfile.TemporaryDirectory() as d:
                state = SimpleNamespace(md=FakeMapData(f, keys[0]), dem=FakeDem(), widths=None, surface=None,
                                        airports=None, sea=None, lock=threading.Lock(), cache_dir=d,
                                        bridges={}, anchor=lambda *args: (None, None))
                cases = ((None, pp.STOCK), (pp.FR_DEMO, pp.choose(pp.FR_DEMO, fid)),
                         (pp.FR_ALL, pp.RECESSED), (pp.FR_DEMO, pp.choose(pp.FR_DEMO, fid)), (None, pp.STOCK))
                with mock.patch.object(blockworker, "_s", state):
                    for i, (profile, expected) in enumerate(cases):
                        out = Path(d, str(i))
                        out.mkdir()
                        blockworker.roads_tile(1, LAT, LON, 15, 0, 0, str(out), "/temp/poc", True, .05,
                                              buildings=True, pole_profile=profile)
                        data = json.loads((out / "roads_1_0_0.json").read_text())
                        self.assertEqual(data["props"]["groups"], [expected.group])
                        self.assertTrue(data["wire"]["shape"])

    def test_worker_cache_separates_profiles_and_forwards_them(self):
        import blockworker
        keys, forwarded = [], []
        def revisit(kind, key, aid, tx, ty, out, vfs, name, build, extra, result):
            keys.append(key)
            return build()
        def build(*args):
            forwarded.append(args[-1])
            return ("/temp/result", 0, [])
        with mock.patch.object(blockworker, "_s", SimpleNamespace(md=None)), \
                mock.patch.object(blockworker, "_revisit_tile", side_effect=revisit), \
                mock.patch.object(blockworker, "_roads_tile", side_effect=build):
            for profile in (None, pp.FR_DEMO, pp.FR_ALL, None):
                blockworker.roads_tile(1, 48.815, 7.79, 15, 0, 0, "unused", "/temp/test", True, 0.,
                                      buildings=True, pole_profile=profile)
        self.assertEqual(keys[0], keys[3])
        self.assertNotEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])
        self.assertNotEqual(keys[1], keys[2])
        self.assertEqual(forwarded, [None, pp.FR_DEMO, pp.FR_ALL, None])

    def test_road_builder_forwards_profile_into_real_placement(self):
        from test_roads import FRAME, features
        cells = [features([[(-200., 50.), (600., 50.)]], ["residential"])]
        seen = []
        original = furniture.place
        def place(*args, **kw):
            seen.append(kw.get("pole_profile"))
            return original(*args, **kw)
        with mock.patch.object(furniture, "place", side_effect=place):
            props, wires = [], []
            roads.build_tile(FRAME, 0, 0, cells, lambda la, lo: np.zeros(np.shape(la)),
                             walks=[], props=props, wires=wires, pole_profile=pp.FR_DEMO)
        self.assertEqual(seen, [pp.FR_DEMO])
        self.assertTrue(props)


class GeometryTest(unittest.TestCase):
    def test_mesh_budget_normals_and_collision(self):
        counts = []
        for m in [mesh.model(i) for i in range(3)] + [mesh.collision()]:
            p, n = np.array(m.p), np.array(m.n)
            t = np.array([q for ts in m.tri.values() for q in ts])
            counts.append(len(t))
            self.assertLess(len(p), 65535)
            self.assertTrue(np.isfinite(p).all())
            self.assertAlmostEqual(p[:, 2].min(), 0.)
            self.assertAlmostEqual(p[:, 2].max(), 8.5)
            np.testing.assert_allclose(np.linalg.norm(n, axis=1), 1., atol=1e-12)
            cross = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
            self.assertTrue((np.linalg.norm(cross, axis=1) > 1e-10).all())
            self.assertTrue(((cross * n[t[:, 0]]).sum(axis=1) > 0).all())
        self.assertLess(counts[0], 1200)
        self.assertLess(counts[1], counts[0])
        self.assertLess(counts[2], 100)
        self.assertEqual(counts[3], 12)
        # Collision hull: after welding its flat-shading duplicates, each edge belongs to two faces.
        m = mesh.collision()
        p = [tuple(np.round(q, 10)) for q in m.p]
        from collections import Counter
        edges = Counter(tuple(sorted((p[a], p[b]))) for ts in m.tri.values() for t in ts
                        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])))
        self.assertTrue(all(v == 2 for v in edges.values()))

    def test_pockets_have_real_depth_and_solid_web(self):
        m = mesh.model(0)
        p = np.array(m.p)
        tri = p[np.array([t for ts in m.tri.values() for t in ts])]
        def first_hit(z):
            # Ray from the road side through X=0: Moller-Trumbore intersections.
            origin, direction = np.array([0., -0.5, z]), np.array([0., 1., 0.])
            e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
            h = np.cross(np.broadcast_to(direction, e2.shape), e2)
            det = (e1 * h).sum(axis=1)
            mask = abs(det) > 1e-10
            inv = np.divide(1., det, out=np.zeros_like(det), where=mask)
            s = origin - tri[:, 0]
            u = inv * (s * h).sum(axis=1)
            q = np.cross(s, e1)
            v = inv * (direction * q).sum(axis=1)
            t = inv * (e2 * q).sum(axis=1)
            mask &= (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0)
            return origin[1] + t[mask].min()
        for a, b in mesh.POCKETS:
            z = (a + b) / 2
            self.assertAlmostEqual(first_hit(z), -mesh.section(z)[1] + .06, places=8)
            self.assertGreater(mesh.section(b)[1] - mesh.DEPTH, 0.02)
        self.assertAlmostEqual(first_hit(1.14), -mesh.section(1.14)[1], places=8)
        self.assertEqual(len(mesh.POCKETS), 13)

    def test_export_materials_lods_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as d:
            mesh.build(d)
            for name in (mesh.NAME + ".dae", "main.materials.json"):
                self.assertEqual(Path(d, name).read_bytes(), (mesh.OUT / name).read_bytes(), name)
            # PNG compression can differ between Pillow/zlib builds. Require
            # exactly the same decoded image, not the same compressed stream.
            with Image.open(Path(d, "concrete_grain.png")) as generated, \
                    Image.open(mesh.OUT / "concrete_grain.png") as shipped:
                for img in (generated, shipped):
                    self.assertEqual(img.format, "PNG")
                    self.assertEqual(img.mode, "RGB")
                    self.assertEqual(img.size, (128, 128))
                self.assertEqual(generated.info, shipped.info, "PNG metadata")
                np.testing.assert_array_equal(np.asarray(generated), np.asarray(shipped),
                                              err_msg="concrete_grain.png pixels")
            xml = ET.parse(Path(d, mesh.NAME + ".dae"))
            ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
            names = {n.attrib.get("name") for n in xml.findall(".//c:node", ns)}
            self.assertTrue({mesh.NAME + str(s) for s in (96, 24, 2)} <= names)
            self.assertIn("collision-1", names)
            self.assertIn("Colmesh_" + mesh.NAME + "-1", names)
            mats = json.loads(Path(d, "main.materials.json").read_text())
            self.assertTrue(set(mesh.model(0).tri) <= set(mats))


if __name__ == "__main__":
    unittest.main()
