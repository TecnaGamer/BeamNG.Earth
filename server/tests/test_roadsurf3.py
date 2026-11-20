"""Road surface v3 phase S2 (roadsurf3.py, earthfast.lane_surface / cut_lines): the ground surface's kernel on small
synthetic cases (strips tiling the road, tapers without a step, the weld onto a road's row, the markings' kinds, the
folds left out, the cut of the tile's own lines with their dash phase). The real-place seam case is test_seamprobe's
(fixtures/seamprobe_v3s2_*.npz, tools/probes/surfprobe.py --fixture)."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import fast  # noqa: E402
import roadsurf3  # noqa: E402

HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadsurf3.SURF_VERSION


def quintic(u):
    u = np.clip(u, 0.0, 1.0)
    return u ** 3 * (10 - 15 * u + 6 * u * u)


def piece(n=51, x0=0.0, kinds=(1, 1, 4), widths=None, step=2.0):
    """One straight piece along +x from x0: rows every step m, boundaries from y = 5 down by each strip's width(s)."""
    s = x0 + np.arange(n) * step
    rows = np.c_[s, s, np.zeros(n)]
    nb = len(kinds) + 1
    t = np.zeros((n, nb))
    t[:, 0] = 5.0
    for k in range(len(kinds)):
        w = widths[k](s - x0) if widths is not None and widths[k] is not None else np.full(n, 3.5)
        t[:, k + 1] = t[:, k] - w
    return rows, t


def inputs(pieces, roads=(), structs=(), shoulders=(1.0, 1.0), country="US"):
    rows = [p[0] for p in pieces]
    ts = [p[1] for p in pieces]
    kinds = [list(p[2]) for p in pieces]
    rxy = [np.asarray(r[0], np.float64) for r in roads]
    sxy = [np.asarray(r[0], np.float64) for r in structs]
    offs = lambda parts: np.r_[0, np.cumsum([len(p) for p in parts])].astype(np.int64)  # noqa: E731
    return {"rows": np.concatenate(rows), "row_off": offs(rows),
            "t": np.concatenate([t.reshape(-1) for t in ts]),
            "t_off": np.r_[0, np.cumsum([t.size for t in ts])].astype(np.int64),
            "kinds": np.asarray(sum(kinds, []), np.int64), "k_off": np.r_[0, np.cumsum([len(k) for k in kinds])],
            "pmeta": np.array([[shoulders[0], shoulders[1], 3.5]] * len(pieces), np.float64),
            "rxy": np.concatenate(rxy) if rxy else np.zeros((0, 2)), "r_off": offs(rxy),
            "r_hw": np.array([r[1] for r in roads], np.float64), "r_oneway": np.array([True] * len(roads), bool),
            "sxy": np.concatenate(sxy) if sxy else np.zeros((0, 2)), "s_off": offs(sxy),
            "s_hw": np.array([r[1] for r in structs], np.float64),
            "box": np.array([-1e5, -1e5, 1e5, 1e5]), "params": roadsurf3.params(country)}


def areas(got):
    v, t = got["mesh"]["v"], got["mesh"]["tri"]
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2.0


@unittest.skipUnless(HAVE, "earthfast >= 550 needed")
class Kernel(unittest.TestCase):
    def test_strips_tile_the_road(self):
        # two lanes and an added lane tapering in over 100 m (quintic), shoulders 1 m: every triangle turned up, the
        # area the strips' (no overlap, no gap)
        rows, t = piece(kinds=(1, 1, 4), widths=(None, None, lambda s: 3.5 * quintic(s / 100.0)))
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 1, 4))]))
        self.assertEqual(got["kept"], [0])
        ar = areas(got)
        self.assertTrue((ar > 0).all())
        self.assertAlmostEqual(float(ar.sum()), 100.0 * (2 + 7.0) + 100.0 * 1.75, delta=1.0)
        kinds = {name for name, *_ in got["lines"]}
        self.assertTrue({"edge", "edge_left", "lane", "exit"} <= kinds, kinds)
        # the added lane's exit dashes start where it is LINE_W_MIN of a lane wide, never in the taper's thin start
        ex = [xy for name, xy, *_ in got["lines"] if name == "exit"]
        w_at = 3.5 * quintic(min(float(xy[0, 0]) for xy in ex) / 100.0)
        self.assertGreaterEqual(w_at, roadsurf3.LINE_W_MIN * 3.5 - 0.3)

    def test_weld_onto_road_rows(self):
        # a road along y = 1.4 (half width 4.0) through a piece from x = 20 to 120: the road is cut at both end rows,
        # and the piece's end rows ARE the road's cut rows (the same points: a shared row, no gap or overlap)
        rows, t = piece(x0=20.0, kinds=(1, 1))
        road = (np.array([[-50.0, 1.4], [200.0, 1.4]]), 4.0)
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 1))], roads=[road]))
        st = got["status"][0]
        self.assertIn("weld_lo:road1", st["flags"])
        self.assertIn("weld_hi:road1", st["flags"])
        v, owner, tri = got["mesh"]["v"], got["mesh"]["owner"], got["mesh"]["tri"]
        for x in (20.0, 120.0):
            for y in (5.4, -2.6):
                hit = np.flatnonzero((np.abs(v[:, 0] - x) < 1e-9) & (np.abs(v[:, 1] - y) < 1e-9))
                users = {int(owner[k]) for k in range(len(tri)) if np.isin(tri[k], hit).any()}
                self.assertEqual(users, {0, -1}, (x, y, users))
        # the road strip has nothing between the cuts
        mid = v[tri[owner == -1]].mean(axis=1)
        self.assertFalse(((mid[:, 0] > 21.0) & (mid[:, 0] < 119.0)).any())
        # no width step: the welded ends ease over at least BLEND_MIN_M
        self.assertLessEqual(st["lane_rate"], 0.06)

    def test_gore_lines_and_chevrons(self):
        # through, gore opening to 4 m, branch: channelizing lines where it is wide, chevrons in it (US), hatching
        # elsewhere
        g = lambda s: 4.0 * quintic(s / 100.0)  # noqa: E731
        rows, t = piece(kinds=(1, 2, 3), widths=(None, g, None))
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 2, 3))]))
        names = [name for name, *_ in got["lines"]]
        self.assertIn("channel", names)
        self.assertGreater(names.count("stripe"), 4)
        got_eu = roadsurf3.run_kernel(inputs([(rows, t, (1, 2, 3))], country="DE"))
        self.assertGreater([n for n, *_ in got_eu["lines"]].count("stripe"), 0)
        # before the gore opens the through / branch boundary is one exit line, not two channel lines
        ex = [xy for name, xy, *_ in got["lines"] if name == "exit"]
        self.assertTrue(ex and min(float(xy[0, 0]) for xy in ex) < 30.0)

    def test_earlier_piece_trims_and_welds(self):
        # two overlapping pieces on one road: the second keeps its rows outside the first and its end lies on the
        # first's end row
        r1, t1 = piece(n=51, x0=0.0, kinds=(1, 1))
        r2, t2 = piece(n=51, x0=60.0, kinds=(1, 1))
        got = roadsurf3.run_kernel(inputs([(r1, t1, (1, 1)), (r2, t2, (1, 1))]))
        st = {s["piece"]: s for s in got["status"]}
        self.assertTrue(st[1]["kept"])
        self.assertIn("weld_lo:piece", st[1]["flags"])
        self.assertLess(float(sum(areas(got))), 160.0 * 9.0 + 1.0)      # (0 .. 160 m once)

    def test_structure_trims(self):
        rows, t = piece(kinds=(1, 1))
        deck = (np.array([[60.0, 1.5], [200.0, 1.5]]), 6.0)
        got = roadsurf3.run_kernel(inputs([(rows, t, (1, 1))], structs=[deck]))
        st = got["status"][0]
        self.assertIn("trim_hi:structure", st["flags"])
        self.assertTrue(any(f.startswith("deck_hi:") for f in st["flags"]), st["flags"])
        self.assertLess(float(got["mesh"]["v"][:, 0].max()), 61.0)

    def test_folds_left_out(self):
        # a road bending back hard: no triangle turned over (roadmesh._keep_up's rule)
        th = np.linspace(0.0, np.pi * 0.9, 40)
        line = np.c_[6.0 * np.cos(th), 6.0 * np.sin(th)]
        got = roadsurf3.run_kernel(inputs([piece(n=3, x0=500.0, kinds=(1,))[0:2] + ((1,),)], roads=[(line, 7.0)]))
        self.assertTrue((areas(got) > 0).all())

    def test_deterministic(self):
        rows, t = piece(kinds=(1, 1, 4), widths=(None, None, lambda s: 3.5 * quintic(s / 100.0)))
        road = (np.array([[-50.0, 1.4], [300.0, 1.4]]), 6.0)
        a = roadsurf3.run_kernel(inputs([(rows, t, (1, 1, 4))], roads=[road]))
        b = roadsurf3.run_kernel(inputs([(rows, t, (1, 1, 4))], roads=[road]))
        self.assertEqual(a["mesh"]["v"].tobytes(), b["mesh"]["v"].tobytes())
        self.assertEqual(a["mesh"]["tri"].tobytes(), b["mesh"]["tri"].tobytes())


@unittest.skipUnless(HAVE, "earthfast >= 550 needed")
class Cuts(unittest.TestCase):
    def test_cut_lines_and_phase(self):
        poly = np.array([[10.0, -5.0], [20.0, -5.0], [20.0, 5.0], [10.0, 5.0]])
        line = {"m": "line_white", "w": 0.15, "n": [[0.0, 0.0, 0.0], [30.0, 0.0, 0.0]], "dash": (3.0, 9.0), "phase": 1.0}
        edge = {"m": "ECA_road_edge_dirt", "w": 1.6, "layer": "edge", "n": [[0.0, 6.0, 0.0], [30.0, 6.0, 0.0]]}
        deck = {"m": "line_white", "w": 0.15, "deck": True, "n": [[0.0, 0.0, 9.0], [30.0, 0.0, 9.0]]}
        wide = np.array([[10.0, -8.0], [20.0, -8.0], [20.0, 8.0], [10.0, 8.0]])
        out = roadsurf3.cut_chunks([line, edge, deck], [poly], [wide], roadsurf3._is_edge)
        lines = [c for c in out if c["m"] == "line_white" and not c.get("deck")]
        self.assertEqual(len(lines), 2)
        a, b = sorted(lines, key=lambda c: c["n"][0][0])
        self.assertAlmostEqual(a["n"][-1][0], 10.0)
        self.assertAlmostEqual(b["n"][0][0], 20.0)
        self.assertAlmostEqual(b["phase"], 1.0 + 20.0)          # (the dash cycle goes on from the line's start)
        self.assertEqual(len([c for c in out if c["m"] == "ECA_road_edge_dirt"]), 2)    # (the wide polygon cuts it)
        self.assertIn(deck, out)


class BuildFaults(unittest.TestCase):
    def test_surface_cache_generation(self):
        import revisit
        self.assertEqual(revisit.keyed_value("BNE_ROADSURF_V3", "off"), "BNE_ROADSURF_V3=off")
        self.assertEqual(revisit.keyed_value("BNE_ROADSURF_V3", "surface"), "BNE_ROADSURF_V3=surface@v4")

    def test_fallback_marks_the_active_tile_short(self):
        from unittest.mock import patch
        import revisit
        for stage in ("tile_surface", "cut_chunks", "surface_chunks"):
            chunks = [{"m": "asphalt", "_v3_fallback": True}]
            with self.subTest(stage=stage), revisit.scope() as tile:
                with patch.object(roadsurf3, "tile_surface", return_value={"cut": [], "cut_wide": []}), \
                        patch.object(roadsurf3, stage, side_effect=RuntimeError("injected")):
                    self.assertIs(roadsurf3.apply(None, None, chunks, None), chunks)
                self.assertEqual(chunks, [{"m": "asphalt"}])
                self.assertFalse(tile.kept_until()[0])

    def test_geo_skin_retries_short_content_and_propagates_the_fault(self):
        import os
        import pickle
        import tempfile
        import blockworker
        import revisit
        key = "v3_skin_fault"
        self.addCleanup(blockworker._geo_inputs.pop, key, None)
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, blockworker.geo_names(key)["inputs"])
            def write(short):
                blockworker._write_bytes(path, pickle.dumps({"surface_short": short}))
            write(True)
            with revisit.scope() as skin:
                self.assertTrue(blockworker._skin_inputs(key, out, lambda: None)["surface_short"])
            self.assertFalse(skin.kept_until()[0])
            with revisit.scope() as retry:
                self.assertFalse(blockworker._skin_inputs(key, out, lambda: write(False))["surface_short"])
            self.assertTrue(retry.complete)

    def test_geo_skin_maps_the_mesh_to_the_anchor(self):
        import blockworker
        import geodesy
        import geotiles
        src, dst = geodesy.EnuFrame(39.25, -76.64), geodesy.EnuFrame(39.26, -76.63)
        xy = np.array([[0., 0.], [5., 0.], [0., 5.]])
        c = {"n": [], "tri": True, "xy": xy, "T": np.array([[0, 1, 2]]), "uv": xy / 5}
        mapped, = blockworker._map_chunks(src, dst, [c])
        np.testing.assert_array_equal(mapped["xy"], geotiles.world_xy_map(src, dst, xy))
        np.testing.assert_array_equal(mapped["uv"], c["uv"])
        np.testing.assert_array_equal(mapped["T"], c["T"])

    def test_lane_revision_changes_during_graph_build(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import mapcells
        import revisit
        home = mapcells.CellKey(14, 3000, 6001)
        feature = SimpleNamespace(_road_revision=0)
        def build(key, cell, memo, read_cells, events_only):
            read_cells.append(key)
            cell(key)._road_revision += 1
            return {}
        with patch.object(roadsurf3.lanegraph, "cell_lanes", side_effect=build) as graph:
            for _ in range(2):
                with revisit.scope() as tile:
                    roadsurf3._cell_lanes(home, lambda _: feature, {})
                self.assertFalse(tile.complete)
            self.assertEqual(graph.call_count, 2)

    def test_dependency_outside_the_home_rings(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import mapcells
        home, copy = mapcells.CellKey(14, 3000, 6000), mapcells.CellKey(14, 3004, 6000)
        f = SimpleNamespace(_road_revision=0)
        def read(key, cell, memo, read_cells, events_only):
            read_cells.extend((home, copy))
            return cell(copy)._road_revision
        with patch.object(roadsurf3.lanegraph, "cell_lanes", side_effect=read) as build:
            def get():
                return roadsurf3._cell_lanes(home, lambda k: f if k == copy else None, {})
            self.assertEqual(get(), 0)
            self.assertEqual(get(), 0)
            f._road_revision += 1
            self.assertEqual(get(), 1)
            self.assertEqual(build.call_count, 2)


@unittest.skipUnless(HAVE, "earthfast 550")
class SurfacePartition(unittest.TestCase):
    def test_overwidth_carriageways_share_a_boundary(self):
        rows, bounds = piece(n=3, x0=500.0, kinds=(1,))
        roads = [(np.array([[0., y], [20., y]]), hw) for y, hw in ((0., 6.), (8., 4.))]
        g = roadsurf3.run_kernel(inputs([(rows, bounds, (1,))], roads=roads))
        mesh = g["mesh"]
        for n, expected in enumerate((6. + 4.8, 4. + 3.2)):
            inside = mesh["owner"] == -1-n
            self.assertAlmostEqual(areas(g)[inside].sum(), 20.*expected, places=7)
        self.assertAlmostEqual(mesh["v"][mesh["tri"][mesh["owner"] == -1], 1].max(), 4.8)

    def test_ramp_preserves_the_nose_and_rejoins_its_mapped_line(self):
        kinds = (1, 2, 3)
        rows, bounds = piece(kinds=kinds, widths=(None, lambda s: 4.*quintic(s/100.), None))
        roads = [(np.array([[80., y], [250., y]]), 2.75) for y in (3.25, -3.25)]
        g = roadsurf3.run_kernel(inputs([(rows, bounds, kinds)], roads))
        m = g["mesh"]
        def edges(owner, x):
            p = m["v"][np.unique(m["tri"][m["owner"] == owner])]
            return np.unique(p[p[:, 0] == x, 1])
        self.assertEqual(edges(-1, 100.)[0] - edges(-2, 100.)[-1], 4.)
        for x in (100., 130., 200., 250.):
            displacement = -3. * quintic(1. - (x - 100.) / 150.)
            np.testing.assert_allclose(edges(-2, x), np.array([-6., -.5]) + displacement, atol=.00051)

    def test_apron_and_terrain_coverage(self):
        rows, bounds = piece(n=3, x0=500.0, kinds=(1,))
        roads = [(np.array([end, [20., 20.]]), 3.) for end in ([0.,20.], [40.,20.], [20.,40.])]
        g = roadsurf3.run_kernel(inputs([(rows, bounds, (1,))], roads=roads))
        m = g["mesh"]
        self.assertTrue((m["kind"] == 5).any())
        geom = m["v"], m["tri"], np.zeros(len(m["tri"]), np.uint8)
        coverage = roadsurf3.paint_coverage(geom, geom[0], 41, 41, True)
        self.assertTrue((coverage[0, 20, :40] == 255).all())
        self.assertTrue((coverage[0, 20:40, 20] == 255).all())
        self.assertEqual(coverage[0, 5, 20], 0)

    def test_paint_uses_the_ground_mesh_built_from_cells(self):
        from types import SimpleNamespace
        from test_paintlist import FakeSurface
        from test_roads import FRAME, LAT, LON, features
        import mapcells
        road = features([[(-32., 0.), (32., 0.)]], ["motorway"])
        home = mapcells.cell_of(LAT, LON)
        def cell(key):
            return road if key == home else None
        surface = FakeSurface([road], [], [])
        surface.md = SimpleNamespace(has=lambda k, layer: True,
                                     cell=lambda k, **kw: SimpleNamespace(layers={"roads": cell(k)}))
        box = (-32., -32., 32., 32.)
        mesh = roadsurf3.tile_surface(FRAME, box, cell, surface.urban)["mesh"]
        xy, tris, concrete = roadsurf3.paint_geometry(surface, FRAME, box, None)
        self.assertGreater(len(tris), 0)
        self.assertEqual(xy.tobytes(), mesh["v"].tobytes())
        self.assertEqual(tris.tobytes(), mesh["tri"].tobytes())
        self.assertFalse(concrete.any())

    def test_terrain_painters_replace_the_previous_outline(self):
        from unittest.mock import patch
        from test_paintlist import FakeSurface
        from test_roads import FRAME, features
        import surface as sf

        # Move the whole surface north. All three painters must remove the old asphalt.
        roads = features([[(-32., 0.), (32., 0.)]], ["motorway"])
        surface = FakeSurface([roads], [], [])
        surface.md = object()
        xy = np.array([[-32., 12.], [32., 12.], [32., 20.], [-32., 20.]])
        geom = xy, np.array([[0, 1, 2], [0, 2, 3]], np.int64), np.zeros(2, np.uint8)
        photo = np.full((64, 64, 3), [20, 110, 30], np.uint8)
        north, east = np.mgrid[-32.:32., -32.:32.]
        lat, lon, _ = FRAME.enu_to_geodetic(east, north, 0.)
        idx = np.full((64, 64), sf.GRASS, np.uint8)
        with patch.object(roadsurf3, "ON", True), patch.object(roadsurf3, "paint_geometry", return_value=geom):
            painted = sf.paint_square(photo, FRAME, -32, -32, 64, surface, simple=True)
            mask = sf.road_mask(FRAME, -32, -32, 64, 64, surface, corners=False)
            mat = sf.paint_roads(idx, lat, lon, [roads], 1., v3=(FRAME, surface))
        self.assertEqual((mask[16, 32], mask[32, 32]), (255, 0))
        self.assertFalse(np.array_equal(painted[16, 32], photo[16, 32]))
        np.testing.assert_array_equal(painted[32, 32], photo[32, 32])
        self.assertEqual((mat[48, 32], mat[32, 32]), (sf.ASPHALT, sf.GRASS))


if __name__ == "__main__":
    unittest.main()
