"""Actual-source tunnel regressions: Obor and held-out tracker #464, plus source splits.

Fixtures contain unchanged Overture 2026-09-23.1 roads (including source record IDs and
linear-referenced flags) and actual zoom-15 DEM samples on an 8 m grid, captured 2026-11-01.
DEM interpolation is fixture-only, not a production fallback or surveyed underground height.
Run: python3 -m unittest discover -s server/tests -p test_tunnel_layering.py
Live-source geometry/mesh report: python3 tools/tunnelprobe.py 44.4499 26.1244 --cache-dir CACHE
Held-out: python3 tools/tunnelprobe.py -31.943509 115.853865 --cache-dir CACHE
These geometry tests do not claim the whole Obor interchange or in-game collision is fixed.
"""
import sys
import unittest
from pathlib import Path

from unittest import mock
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import grading
import mapgeom
import navgraph
import roadmesh
import roads
from test_roads import FRAME, features, rules


OBOR = ("75d3e458-4976-4995-8786-6278090ece97", "7d55c224-9513-4802-8ec1-7621b8d1d8b9")
NORTHBRIDGE = ("235567c9-1d08-4994-b480-4203b19db1fa", "bb7f3e16-ec3b-489b-b564-d9fa8215af7d")


def captured(name):
    with np.load(Path(__file__).with_name("fixtures") / (name + ".npz"), allow_pickle=False) as data:
        arrays = {k: data[k] for k in data.files}
    frame = geodesy.EnuFrame(*arrays["center"])
    feats = mapgeom.Features.from_arrays("roads", arrays, "roads.")
    gx, gy, grid = arrays["grid_x"], arrays["grid_y"], arrays["grid_z"]

    def elev(la, lo):
        shape = np.shape(la)
        x, y, _ = frame.geodetic_to_enu(np.asarray(la).ravel(), np.asarray(lo).ravel(), 0.0)
        if np.any((x < gx[0]) | (x > gx[-1]) | (y < gy[0]) | (y > gy[-1])):
            raise ValueError("source probe exceeded captured DEM rectangle")
        ix = np.clip(np.searchsorted(gx, x) - 1, 0, len(gx) - 2)
        iy = np.clip(np.searchsorted(gy, y) - 1, 0, len(gy) - 2)
        tx, ty = (x - gx[ix]) / (gx[ix + 1] - gx[ix]), (y - gy[iy]) / (gy[iy + 1] - gy[iy])
        z = ((1 - tx) * (1 - ty) * grid[iy, ix] + tx * (1 - ty) * grid[iy, ix + 1] +
             (1 - tx) * ty * grid[iy + 1, ix] + tx * ty * grid[iy + 1, ix + 1])
        return z.reshape(shape)
    tp = roads.TunnelProfiles(frame, lambda key: feats, elev)
    tp.raw = elev
    return frame, feats, elev, tp


def selected(feats, ids):
    """Retain exact captured source rows/rules without changing geometry or fractions."""
    rows = sorted(feats.index(fid) for fid in ids)
    remap = {old: new for new, old in enumerate(rows)}
    coords, po, ro, co = [], [0], [0], [0]
    for i in rows:
        for part in feats.parts(i):
            for ring in part:
                coords.extend(ring)
                co.append(len(coords))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    tables = {}
    for name, table in feats.rules.items():
        keep = np.isin(table.feature, rows)
        tables[name] = mapgeom.RuleTable(np.array([remap[int(i)] for i in table.feature[keep]], np.int32),
                                        table.value[keep], table.start[keep], table.end[keep])
    return mapgeom.Features("roads", feats.ids[rows], feats.kind[rows], np.array(po), np.array(ro), np.array(co),
                            np.asarray(coords), attrs={k: v[rows] for k, v in feats.attrs.items()}, rules=tables)


class SourceTunnelLayers(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("roadz.PROVIDER", None))

    def test_obor_both_mapped_bores_are_continuously_below_the_roof(self):
        frame, feats, elev, tp = captured("obor_tunnels")
        box = (-500.0, -500.0, 500.0, 500.0)
        closed = tp.parts([feats], box)
        for source in OBOR:
            with self.subTest(source=source):
                parts = [p for p in closed if p[0].startswith(source + ".")]
                self.assertTrue(parts, "baseline lower DEM hull erased this mapped bore entirely")
                i = feats.index(source)
                ring = roads.tunnel_parts(feats, i)[0]
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = roads.densify(np.c_[e, n], 2.0)
                floors = np.array([roads.tunnel_floor_at(parts, *p) for p in xy], dtype=float)
                self.assertTrue(np.isfinite(floors).all(), "no open gap inside the mapped bore")
                la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                ground = frame.geodetic_to_enu(la, lo, elev(la, lo))[2]
                self.assertGreater(float(np.min(ground - floors)), roadmesh.TUNNEL_H + 0.5)
        chunks = roads._tunnel_chunks(tp, [feats], box)
        self.assertTrue(any(c.get("tunnel") and c.get("deck") for c in chunks))
        mesh = roadmesh.tunnel(chunks, (0.0, 0.0, 0.0))
        self.assertIsNotNone(mesh)
        self.assertTrue(np.isfinite(mesh[0]).all())
        # The covered interior cannot punch the surface road out of the terrain.
        xs = np.arange(-400.0, 400.0, 2.0)
        xx, yy = np.meshgrid(xs, xs)
        la, lo, _ = frame.enu_to_geodetic(xx.ravel(), yy.ravel(), 0.0)
        u = frame.geodetic_to_enu(la, lo, elev(la, lo))[2].reshape(xx.shape)
        holes = roads.tunnel_hole_mask(closed, u, 0.0, 0.0, 2.0, tp.heights, tp.mouths)
        for la, lo in ((44.4496548171, 26.1242012230), (44.4498027493, 26.1244340945)):
            x, y, _ = frame.geodetic_to_enu(la, lo, 0.0)
            ix, iy = int(np.floor((x - xs[0]) / 2)), int(np.floor((y - xs[0]) / 2))
            self.assertFalse(holes[iy, ix], "actual Piața Obor surface crossing remains solid")

    def test_surface_endpoint_grading_cannot_move_a_mapped_bore_or_its_roof(self):
        frame, feats, elev, tp = captured("obor_tunnels")
        box = (-500.0, -500.0, 500.0, 500.0)
        reference = tp.parts([feats], box)
        graded = roads.TunnelProfiles(frame, lambda _key: feats, elev)
        graded.raw = elev
        graded.ends = lambda la, lo: elev(la, lo) + 2.0
        changed = graded.parts([feats], box)
        self.assertEqual([p[0] for p in reference], [p[0] for p in changed])
        for first, second in zip(reference, changed):
            np.testing.assert_allclose(first[2], second[2], atol=1e-8)
            np.testing.assert_allclose(first[3], second[3], atol=1e-8,
                                       err_msg="graded approach feedback moved the covered layer")

    def test_obor_portals_have_colliding_open_floor_without_roof_lattice_wedges(self):
        import cdae
        import finepatch
        import heightmap
        import tempfile
        frame, feats, elev, tp = captured("obor_tunnels")
        box = (-500.0, -500.0, 500.0, 500.0)
        closed, opened = tp.parts([feats], box), tp.open_parts([feats], box)
        for fine_on in (False, True):
            for fid, _sp, xy, z, _lon in opened:
                mouths = [np.asarray(p[2])[at] for p in closed if p[0].split("#")[0] == fid.split("#")[0]
                          for at, mouth in zip((0, -1), tp.mouth(p[0])) if mouth]
                for end in (0, -1):
                    if not any(np.linalg.norm(xy[end] - p) < 1e-5 for p in mouths):
                        continue
                    with self.subTest(fid=fid, end=end, fine=fine_on):
                        size = 64
                        cx, cy = np.round(np.asarray(xy)[end] / 2) * 2
                        xs, ys = heightmap.sample_coords(cx, cy, size)
                        xx, yy = np.meshgrid(xs, ys)
                        la, lo, _ = frame.enu_to_geodetic(xx, yy, np.zeros(xx.shape))
                        # Actual captured DEM is still at roof height here: the floor
                        # consumer must not pin its corners to that upper terrain layer.
                        u = heightmap.surface_u(frame, elev, xs, ys)
                        keep = {"u": u.copy(), "lat": la, "lon": lo}
                        holes = roads.block_tunnel_holes(tp, lambda _key: feats, frame, keep, cx, cy, 2.0)
                        idx = np.zeros(u.shape, np.uint8)
                        if holes is not None:
                            idx[holes] = heightmap.TER_HOLE
                        info, meshes = {}, []
                        with tempfile.TemporaryDirectory() as directory, mock.patch.object(finepatch, "FINE_ON", fine_on):
                            finepatch.apply(frame, elev, elev, keep, cx, cy, size, directory, "portal", 1, idx, info)
                            self.assertIn("fine", info, "portal collision fill is mandatory even with fine ground off")
                            for part in info["fine"]["parts"]:
                                if not part.get("collide", True):
                                    continue
                                shape = cdae.read(str(Path(directory) / part["shape"]))
                                for mesh in shape["meshes"]:
                                    if mesh["type"] == 4:
                                        continue
                                    arrays = cdae.mesh_arrays(mesh)
                                    vertices = arrays["verts"] + np.asarray(part["pos"])
                                    meshes.append(vertices[arrays["indices"].reshape(-1, 3)])
                        np.testing.assert_array_equal(keep["u"], u, "never lower the covered terrain's height field")
                        triangles = np.concatenate(meshes)
                        path = np.c_[xy, z][::-1] if end == -1 else np.c_[xy, z]
                        for distance in (0.25, 0.75, 1.5, 3., 5., 10., 20.):
                            p = roads._along(path, distance)
                            a = triangles[:, 0, :2]
                            ab, ac = triangles[:, 1, :2] - a, triangles[:, 2, :2] - a
                            det = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
                            valid = np.abs(det) > 1e-8
                            q = p[:2] - a
                            b = np.divide(q[:, 0] * ac[:, 1] - q[:, 1] * ac[:, 0], det,
                                          out=np.zeros(len(det)), where=valid)
                            c = np.divide(ab[:, 0] * q[:, 1] - ab[:, 1] * q[:, 0], det,
                                          out=np.zeros(len(det)), where=valid)
                            on = valid & (b >= -1e-5) & (c >= -1e-5) & (b + c <= 1 + 1e-5)
                            self.assertTrue(on.any(), f"no collision fill {distance} m outside {fid}")
                            heights = (triangles[:, 0, 2] + b * (triangles[:, 1, 2] - triangles[:, 0, 2]) +
                                       c * (triangles[:, 2, 2] - triangles[:, 0, 2]))[on]
                            np.testing.assert_allclose(heights, p[2], atol=0.06,
                                                       err_msg=f"roof lattice intrudes {distance} m outside {fid}")
                        # Verify the portal mouth interior: terrain holes must not be spared,
                        # and no colliding fine triangles may stand inside vehicle clearance.
                        matching_closed = [p for p in closed if p[0].split("#")[0] == fid.split("#")[0]
                                           and (np.linalg.norm(np.asarray(p[2])[0] - xy[end]) < 1e-5 or
                                                np.linalg.norm(np.asarray(p[2])[-1] - xy[end]) < 1e-5)]
                        for cp in matching_closed:
                            in_path = np.c_[cp[2], cp[3]] if np.linalg.norm(cp[2][0] - xy[end]) < 1e-5 else np.c_[cp[2], cp[3]][::-1]
                            for in_dist in (0.5, 1.0, 2.0, 3.0):
                                pin = roads._along(in_path, in_dist)
                                ix = int(np.floor((pin[0] - xs[0]) / 2.0))
                                iy = int(np.floor((pin[1] - ys[0]) / 2.0))
                                if 0 <= ix < size and 0 <= iy < size and u[iy, ix] < pin[2] + roadmesh.TUNNEL_H + 0.3:
                                    self.assertEqual(idx[iy, ix], heightmap.TER_HOLE,
                                                     f"terrain hole inside mouth at -{in_dist}m was wrongly spared")
                                if len(triangles):
                                    a = triangles[:, 0, :2]
                                    ab, ac = triangles[:, 1, :2] - a, triangles[:, 2, :2] - a
                                    det = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
                                    valid = np.abs(det) > 1e-8
                                    q = pin[:2] - a
                                    b = np.divide(q[:, 0] * ac[:, 1] - q[:, 1] * ac[:, 0], det,
                                                  out=np.zeros(len(det)), where=valid)
                                    c = np.divide(ab[:, 0] * q[:, 1] - ab[:, 1] * q[:, 0], det,
                                                  out=np.zeros(len(det)), where=valid)
                                    on = valid & (b >= -1e-5) & (c >= -1e-5) & (b + c <= 1 + 1e-5)
                                    if on.any():
                                        hts = (triangles[:, 0, 2] + b * (triangles[:, 1, 2] - triangles[:, 0, 2]) +
                                               c * (triangles[:, 2, 2] - triangles[:, 0, 2]))[on]
                                        self.assertFalse(np.any((hts > pin[2] + 0.1) & (hts < pin[2] + roadmesh.TUNNEL_H - 0.2)),
                                                         f"colliding fine wedge stands inside portal at -{in_dist}m")

    def test_northbridge_both_main_bores_exist_and_source_joint_is_not_a_portal(self):
        _frame, feats, _elev, tp = captured("northbridge_tunnels")
        parts = tp.parts([feats], (-300, -300, 300, 300))
        for source in NORTHBRIDGE:
            bore = [p for p in parts if p[0].startswith(source + ".")]
            self.assertTrue(bore, source)
            for fid, _sp, xy, z, _lo in bore:
                self.assertGreater(float(np.min(tp._raw_ground(np.asarray(xy)) - z)), roadmesh.TUNNEL_H + 0.5)
                self.assertTrue(np.isfinite(z).all())
        # The exact source joint at 115.8540771,-31.9435998 joins the long bore to its
        # mapped approach part (72763601...), not a new portal across the carriageway.
        joined = [p for p in parts if p[0].startswith("235567c9-") or p[0].startswith("72763601-")]
        joint = tp.frame.geodetic_to_enu(-31.9435998, 115.8540771, 0.0)
        ends = []
        for fid, _sp, xy, z, _lo in joined:
            for at, mouth in zip((0, -1), tp.mouth(fid)):
                if np.linalg.norm(np.asarray(xy)[at] - joint[:2]) < 0.01:
                    self.assertFalse(mouth)
                    ends.append(float(z[at]))
        self.assertGreaterEqual(len(ends), 2)
        self.assertLess(np.ptp(ends), 1e-5)

    def test_grading_and_meshes_use_the_same_obor_sections(self):
        import geotiles
        _capture_frame, feats, elev, _tp = captured("obor_tunnels")
        feats = selected(feats, (*OBOR, "6d3cc7e9-7354-45d6-80f8-0085c3572f69"))
        # Real consumers use PLAN_Z and CHAIN_Z frames, not a TunnelProfiles in the
        # same artificial frame on both sides (which missed two samples of portal drift).
        frame = geotiles.region_frame(*geotiles.region_of(44.4499, 26.1244))
        def setup(profile):
            profile.raw = elev
        chains = roads.ChainStore(roads.TunnelProfiles, lambda _key: feats, elev, setup)
        tp = roads.ChainView(chains, frame)
        from types import SimpleNamespace
        planner = grading._Planner(frame, SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"roads": feats})), elev)
        secs = planner._tunnel_sections(feats)
        closed = tp.parts([feats], (-np.inf, -np.inf, np.inf, np.inf))
        for source in OBOR:
            expected = [p for p in closed if p[0].startswith(source + ".")]
            actual = [s for s in secs if s[0].startswith(source + ".") and s[4]]
            self.assertEqual(len(actual), len(expected))
            for a, b in zip(actual, expected):
                np.testing.assert_allclose(a[2], b[2], atol=1e-6)
                measured = frame.enu_to_geodetic(b[2][:, 0], b[2][:, 1], b[3])[2]
                np.testing.assert_allclose(a[3], measured, atol=1e-6)
        opened = tp.open_parts([feats], (-np.inf, -np.inf, np.inf, np.inf))
        for fid, _spec, xy, z, _lon in opened:
            actual = [s for s in secs if s[0] == fid.split("#")[0] and s[4] == 0]
            found = [s for s in actual if np.linalg.norm(s[2][0] - xy[0]) < 1e-5 and
                     np.linalg.norm(s[2][-1] - xy[-1]) < 1e-5]
            self.assertEqual(len(found), 1, f"planner and ChainView disagree on open mouth {fid}")
            np.testing.assert_allclose(found[0][2], xy, atol=1e-6)
            expected = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], z)[2]
            np.testing.assert_allclose(found[0][3], expected, atol=1e-6)


class SourceJointAndApproach(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("roadz.PROVIDER", None))

    def test_split_hill_has_one_floor_and_only_two_outer_portals(self):
        f = features([[(-200, 0), (0, 0)], [(0, 0), (200, 0)]], ["primary"] * 2,
                     flags=[(0, "is_tunnel"), (1, "is_tunnel")])
        def elev(la, lo):
            e, _n, _z = FRAME.geodetic_to_enu(la, lo, 0.0)
            return 10.0 + np.maximum(0.0, 60.0 - np.abs(e) * 0.3)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        closed = tp.parts([f], (-300, -100, 300, 100))
        at_joint = []
        for fid, _sp, xy, z, _lon in closed:
            for at, mouth in zip((0, -1), tp.mouth(fid)):
                if np.linalg.norm(xy[at]) < 0.01:
                    self.assertFalse(mouth)
                    at_joint.append(float(z[at]))
        self.assertEqual(len(at_joint), 2)
        self.assertLess(np.ptp(at_joint), 1e-6)
        self.assertLess(max(at_joint), 11.0)

    def test_crossing_constraint_is_not_a_blanket_lowering(self):
        s = np.arange(0.0, 600.1, 2.0)
        ground = np.full(len(s), 20.0)
        mapped = (s >= 200) & (s <= 400)
        kinds = np.zeros(len(s), np.int64)
        floor, closed = roads.mapped_tunnel_floor(s, ground, kinds, mapped, [(290, 310)])
        self.assertTrue((closed[mapped] == 2).all())
        self.assertTrue((closed[~mapped] == 0).all())
        self.assertTrue(np.allclose(floor[mapped], 12.0))
        self.assertEqual(floor[0], 20.0)
        self.assertEqual(floor[-1], 20.0)
        self.assertLessEqual(float(np.max(np.abs(np.diff(floor)) / np.diff(s))), roads.TUNNEL_APPROACH_GRADE + 1e-6)
        untouched, opened = roads.mapped_tunnel_floor(s, ground, kinds, mapped, [])
        np.testing.assert_array_equal(untouched, ground)
        np.testing.assert_array_equal(opened, kinds)

    def test_approach_updates_only_the_source_road_not_a_surface_crossing(self):
        from types import SimpleNamespace
        planner = grading._Planner(FRAME, SimpleNamespace(), lambda la, lo: np.full(np.shape(la), 20.0))
        planner._tunnel_owners = {"bore.0": {"own"}}
        own = grading.Plan(np.array([[-20., 0.], [0., 0.], [20., 0.]]), np.full(3, 20.), 3.5)
        surface = grading.Plan(np.array([[0., -20.], [0., 0.], [0., 20.]]), np.full(3, 20.), 3.5)
        spec = roads.Spec(7., "paved", False, "primary")
        secs = [("bore.0", spec, own.xy[:2].copy(), np.array([20., 16.]), 0),
                ("bore.0", spec, own.xy[1:].copy(), np.array([16., 12.]), 2)]
        planner._tunnel_approaches([("own", own), ("surface", surface)], secs)
        np.testing.assert_allclose(own.z, [20, 16, 12])
        np.testing.assert_allclose(surface.z, [20, 20, 20])

    def test_navigation_uses_solved_floor_without_joining_surface_crossing(self):
        f = features([[(-200, 0), (200, 0)], [(0, -100), (0, 100)]], ["primary", "secondary"],
                     flags=[(0, "is_tunnel", .25, .75)])
        f.rules["connectors"] = rules([(0, "a", 0, 0), (0, "b", 1, 1), (1, "c", 0, 0), (1, "d", 1, 1)])
        floors = {"id000": [(np.array([[-200., 0.], [-100., 0.], [100., 0.], [200., 0.]]),
                              np.array([20., 12., 12., 20.]))]}
        graph = navgraph.build(FRAME, [f], lambda la, lo: np.full(np.shape(la), 20.), (0, 0), tunnel_floors=floors)
        below = [p for name, p in graph["nodes"].items() if "id000" in name and abs(p[0]) <= 100]
        self.assertTrue(below)
        self.assertTrue(all(abs(p[2] - 12.) < .05 for p in below))
        self.assertFalse(any("id000" in e[0] and "id001" in e[1] for e in graph["edges"]))


if __name__ == "__main__":
    unittest.main()
