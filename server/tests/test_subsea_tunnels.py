"""Underwater tunnel consumers: synthetic geography, real generated profiles and water-volume bounds."""
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads
import grading
import geotiles
import roadmesh
import roadz
from test_roads import FRAME, LON, M_LON, features
from test_server import ServerHarness


class SubseaProfileTest(unittest.TestCase):
    def setup_bay(self, reverse=False, surface=None):
        lines = [[(-1200., 0.), (-400., 0.)], [(-400., 0.), (400., 0.)], [(400., 0.), (1200., 0.)]]
        if reverse: lines[1].reverse()
        f = features(lines, ["primary"] * 3, flags=[(i, "is_tunnel") for i in range(3)],
                     surfaces=[(i, surface) for i in range(3)] if surface else ())
        def ground(lat, lon):
            x = (np.asarray(lon) - LON) * M_LON
            return np.where(np.abs(x) < 600., -30., 5.)
        def water(lat, lon):
            x = (np.asarray(lon) - LON) * M_LON
            d = np.where(np.abs(x) < 600., 30., 0.)
            return d, np.where(d > 0, 0., np.nan)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, ground)
        tp.raw = ground
        tp.wet = lambda lat, lon: water(lat, lon)[0]
        tp.water = water
        return tp, f

    def test_a_mapped_subsea_chain_is_built_including_its_landward_parts(self):
        tp, f = self.setup_bay()
        parts = tp.parts([f], (-1300., -100., 1300., 100.))
        self.assertTrue(parts, "a mapped subsea tunnel must not be discarded solely because its chain crosses water")
        self.assertGreaterEqual(len(parts), 3)
        samples = [(xy, z) for _fid, _spec, xy, z, _lon in parts]
        inside = np.concatenate([z[np.abs(xy[:, 0]) < 500.] for xy, z in samples])
        self.assertTrue(len(inside))
        self.assertLessEqual(float(inside.max()), -30. - roads.TUNNEL_COVER + .02)

    def test_reversed_source_parts_have_one_floor_and_no_internal_portals(self):
        tp, f = self.setup_bay(reverse=True)
        tp.parts([f], (-1300., -100., 1300., 100.))
        a, b, c = [tp.done[(str(fid), 0)] for fid in f.ids]
        self.assertAlmostEqual(a[2][-1], b[2][-1], delta=1e-5)
        self.assertAlmostEqual(b[2][0], c[2][0], delta=1e-5)
        for pid, sections in tp.sections.items():
            for index, (a, b, kind) in enumerate(sections):
                if not kind: continue
                mouth = tp.mouth(f"{pid}#{index}")
                if pid.startswith(str(f.ids[1]) + "."): self.assertEqual(mouth, (False, False))
        for _sp, xy, z, _lo in tp.done.values():
            s = np.r_[0., np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            self.assertLessEqual(np.max(np.abs(np.diff(z) / np.diff(s))), .08001)

    def test_grading_reads_the_same_region_profile_without_reentering_a_grader(self):
        tp, f = self.setup_bay()
        md = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"roads": f}))
        pl = grading._Planner(FRAME, md, tp.raw)
        pl.wet, pl.water, pl.tunnel_raw = tp.wet, tp.water, tp.raw
        portals = {}
        sections = pl._tunnel_sections(f, portals=portals)
        self.assertTrue(sections)
        def setup(st):
            st.raw, st.water, st.wet = tp.raw, tp.water, tp.wet
        cs = roads.ChainStore(roads.TunnelProfiles, lambda _key: f, tp.raw, setup)
        mesh = roads.ChainView(cs, FRAME)
        covered = mesh.parts([f], (-1300., -100., 1300., 100.))
        self.assertTrue(covered)
        by_id = {fid: (xy, z) for fid, _sp, xy, z, _lo in covered}
        for fid, _sp, xy, z, kind in sections:
            if not kind: continue
            mxy, mz = by_id[fid]
            la, lo, _h = geotiles.world_to_geodetic(FRAME, np.c_[mxy, mz])
            datum = FRAME.geodetic_to_enu(la, lo, z + grading.ROAD_LIFT)[2]
            np.testing.assert_allclose(datum, mz, atol=1e-6)
            p = grading.Plan(xy, z, 5.)
            physical = grading.grade_points(xy[:, 0], xy[:, 1], np.zeros(len(xy)), [p], grading.ROAD_LIFT)
            expected = FRAME.geodetic_to_enu(la, lo, physical)[2]
            # The existing local grade_points blend averages adjacent segments near sample boundaries.
            # Check its physical handoff separately from the exact shared profile datum above.
            np.testing.assert_allclose(expected, mz, atol=.02)
            np.testing.assert_allclose(xy, mxy, atol=1e-6)
        mouths = pl._mouth_plans(sections, portals)
        self.assertTrue(mouths)
        self.assertFalse(any(fid.startswith(str(f.ids[1]) + ".0#") for fid, _p in mouths),
                         "source joints must not cut new mouths")

    def test_missing_surface_datum_keeps_the_safe_exclusion(self):
        tp, f = self.setup_bay()
        water = tp.water
        tp.water = lambda la, lo: (water(la, lo)[0], np.full(np.shape(la), np.nan))
        self.assertEqual(tp.parts([f], (-1300., -100., 1300., 100.)), [])

    def test_solver_failure_does_not_publish_a_flat_road_across_the_bay(self):
        tp, f = self.setup_bay()
        with patch("subsea._profiles", side_effect=RuntimeError("infeasible fixture")):
            self.assertEqual(tp.parts([f], (-1300., -100., 1300., 100.)), [])

    def test_infeasible_cover_and_grade_do_not_publish_an_infeasibility_certificate(self):
        tp, f = self.setup_bay()
        old_ground, old_water = tp.raw, tp.water
        tp.raw = lambda la, lo: np.where(old_ground(la, lo) < 0, -250., 5.)
        tp.water = lambda la, lo: (np.where(old_water(la, lo)[0] > 0, 250., 0.), old_water(la, lo)[1])
        self.assertEqual(tp.parts([f], (-1300., -100., 1300., 100.)), [])

    def test_real_mesh_consumers_and_serialized_protection_cover_the_bore(self):
        tp, f = self.setup_bay()
        chunks = roads._tunnel_chunks(tp, [f], (-128., -128., 128., 128.))
        self.assertTrue(chunks)
        floor = roadmesh.deck(chunks, (0., 0., 0.))
        tube = roadmesh.tunnel(chunks, (0., 0., 0.))
        self.assertIsNotNone(floor, "the drivable deck must be emitted as well as the walls")
        self.assertIsNotNone(tube)
        self.assertLess(float(floor[0][:, 2].max()), -30.)
        with tempfile.TemporaryDirectory() as tmp:
            roads.write_tile(tmp, "/test", 1, 0, 0, chunks)
            data = json.loads((Path(tmp) / "roads_1_0_0.json").read_text())
        self.assertTrue(data["waterguard"])
        for r in data["waterguard"]:
            xy = np.array(r["nodes"])
            self.assertGreaterEqual(r["r"], 5.)
            self.assertGreater(r["h"], 6.)
            self.assertLess(xy[:, 2].max(), -30.)
            # Full sections survive tile clipping: no water can leak in at a tile border.
            self.assertGreater(np.ptp(xy[:, 1]) * M_LON, 700.)

    def test_terrain_painted_surfaces_still_have_an_underground_collision_floor(self):
        for surface in ("grass", "rock"):
            with self.subTest(surface=surface):
                tp, f = self.setup_bay(surface=surface)
                chunks = roads._tunnel_chunks(tp, [f], (-128., -128., 128., 128.), terrain_paint=True)
                self.assertTrue(any(c.get("tunnel") and c.get("deck") for c in chunks))
                self.assertIsNotNone(roadmesh.deck(chunks, (0., 0., 0.)))


class SubseaHandshakeTest(ServerHarness):
    def test_an_older_mod_still_gets_its_world_and_is_told(self):
        # (a mod without the water guard: told its underwater tunnels may flood, but its world loads: refusing every
        # world job left BeamMP players, whose mod is the server's copy, in an empty world, 2026-11-07)
        self.server.sea = object()
        hello = {"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0", "vegFlat": True}
        self.c.send(hello)
        m, _seen = self.c.until(lambda m: m["type"] == "status" and "older than your app" in m.get("text", ""))
        self.assertIn("older than your app", m["text"])
        poses = []
        self.conn().on_pose = poses.append
        self.c.send({"type": "pose"})
        self.c.send(dict(hello, waterguard=1))  # FIFO acknowledgement: the old client's pose was processed first
        m, _seen = self.c.until(lambda m: m["type"] == "hello" and "waterguard" in m)    # (the older one's reply first)
        self.assertEqual(m["waterguard"], 1)
        self.assertEqual(len(poses), 1, "the older mod's pose was handled")



class CapturedRyfylkeTest(unittest.TestCase):
    def test_long_captured_3d_bore_uses_the_survey_below_an_inferred_bed(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "ryfylke_nvdb.json").read_text())
        rows = roadz.parse_nvdb(fixture["objects"])
        bores = [r for r in rows if r[2] < 0]
        self.assertEqual(len(bores), 2)
        # Original survey geometry; the DEM/water here deliberately are a model, not a captured game world.
        f = features([[(0., 0.), (1., 0.)]] * len(bores), ["primary"] * len(bores),
                     flags=[(i, "is_tunnel") for i in range(len(bores))])
        f.lonlat = np.concatenate([r[0] for r in bores])
        f.coord_off = np.r_[0, np.cumsum([len(r[0]) for r in bores])].astype(np.int64)
        frame = geotiles.frame(*geotiles.tile_of(59.038578, 5.932688, geotiles.CHAIN_Z), geotiles.CHAIN_Z)
        portals = np.array([bores[0][0][0], bores[-1][0][-1]])
        pe, pn, _ = frame.geodetic_to_enu(portals[:, 1], portals[:, 0], 0.)
        ports = np.c_[pe, pn]
        ground_datum = {(round(float(la), 8), round(float(lo), 8)): float(z)
                        for ll, zs, lvl, _ref, _name in rows if lvl == 0
                        for (lo, la), z in zip(ll, zs)}
        def water(la, lo):
            e, n, _ = frame.geodetic_to_enu(la, lo, 0.)
            q = np.c_[np.ravel(e), np.ravel(n)]
            inland = np.min(np.linalg.norm(q[:, None] - ports[None], axis=2), axis=1) < 1000.
            d = np.where(inland, 0., 80.).reshape(np.shape(la))
            return d, np.where(d > 0, 0., np.nan)
        def ground(la, lo, *_args):
            d = water(la, lo)[0]
            z = np.where(d > 0, -80., 5.)
            return np.array([ground_datum.get((round(float(a), 8), round(float(b), 8)), float(c))
                             for a, b, c in zip(np.ravel(la), np.ravel(lo), np.ravel(z))]).reshape(np.shape(la))
        provider = roadz.RoadZ(None, object(), SimpleNamespace(dem=SimpleNamespace(sample=ground)))
        provider.enabled = True
        provider.lines = lambda _src, _key: roadz.Lines.build(rows)
        tp = roads.TunnelProfiles(frame, lambda _key: f, ground)
        tp.raw, tp.water, tp.wet = ground, water, lambda la, lo: water(la, lo)[0]
        # The existing endpoint grader is deliberately unusable: the wet path must not recurse into planning.
        tp.ends = lambda *_args: self.fail("wet profile re-entered the endpoint grader")
        with patch.object(roadz, "PROVIDER", provider):
            parts = tp.parts([f], (-20000., -20000., 20000., 20000.))
        self.assertTrue(parts)
        self.assertEqual(len(tp._subsea), 2)
        h = np.concatenate([geotiles.world_to_geodetic(frame, np.c_[xy, z])[2]
                            for _fid, _spec, xy, z, _lon in parts])
        self.assertLess(float(h.min()), -280., "the survey must not be replaced by an inferred -88 m floor")
        errors = []
        length = 0.
        for _spec, xy, z, _lon in tp.done.values():
            length += float(np.hypot(*np.diff(xy, axis=0).T).sum())
            observed = provider.observed(frame, xy, 5., -1, "")
            self.assertIsNotNone(observed)
            actual = geotiles.world_to_geodetic(frame, np.c_[xy, z])[2]
            errors.extend(np.abs(actual - observed)[np.isfinite(observed)])
        self.assertGreater(length, 14000.)
        self.assertLess(float(np.median(errors)), 1., "the constrained profile should follow the captured survey")


if __name__ == "__main__": unittest.main()
