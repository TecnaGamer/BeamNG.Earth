"""finepatch: a block's sharp graded cells as holes filled by a finer mesh of the same ground (earthfast fine_*)."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import finepatch  # noqa: E402


class FineCellsTest(unittest.TestCase):
    def test_a_graded_step_its_neighbours_and_no_natural_cliff(self):
        """A 3 m cut between columns 5 and 6 (graded: the natural ground was a slope) is fine with a cell round it;
        the same step in the natural ground (raw = u) is the terrain's."""
        u = np.zeros((12, 12))
        u[:, 6:] = 3.0
        raw = np.tile(np.linspace(0, 3, 12), (12, 1))
        c = np.asarray(fast._ef.fine_cells(u, raw, 1.5, 0.5, 1))
        self.assertTrue(c[:, 5].all())                       # the step's cells
        self.assertTrue(c[:, 4].all() and c[:, 6].all())     # grown by one
        self.assertFalse(c[:, 2].any() or c[:, 9].any())
        self.assertFalse(np.asarray(fast._ef.fine_cells(u, u.copy(), 1.5, 0.5, 1)).any(), "a natural cliff")

    def test_holes_at_each_fine_cells_south_west_sample(self):
        """The engine skips a square by its south-west sample: every fine cell's, no other."""
        cells = np.zeros((6, 6), np.uint8)
        cells[1:4, 1:4] = 1
        h = np.asarray(fast._ef.fine_holes(cells))
        self.assertEqual(h.shape, (7, 7))
        self.assertEqual(sorted(zip(*np.nonzero(h))), [(j, i) for j in range(1, 4) for i in range(1, 4)])

    def test_gaps_closed_and_rim(self):
        """A lone terrain cell inside a patch is fine too (closed); the rim: the cells whose layer blend can read the
        patch's holes (each fine cell's south-west sample), the terrain shader's warp of half a sample with it: one
        cell north / east of the patch, two west / south."""
        u = np.zeros((14, 14))
        raw = u.copy()
        u[6, 5] = u[6, 7] = 4.0                    # two sharp spots, one cell apart
        c = np.asarray(fast._ef.fine_cells(u, raw, 1.5, 0.5, 1))
        self.assertTrue(c[5:8, 4:8].all(), "the gap between them filled")
        rim = np.asarray(fast._ef.fine_rim(c.astype(np.uint8)))
        cov = c.astype(bool) | rim.astype(bool)
        for j, i in zip(*np.nonzero(c)):           # every cell with one of its holes for a corner is covered
            self.assertTrue(cov[j - 1, i] and cov[j, i - 1] and cov[j - 1, i - 1])
        J, I = np.nonzero(c)
        self.assertFalse(rim[J.max() + 2:].any() or rim[:, I.max() + 2:].any(), "none past a cell north or east")
        self.assertFalse(rim[:J.min() - 2].any() or rim[:, :I.min() - 2].any(), "none past two cells west or south")
        self.assertTrue(rim[J.min() - 2, I.min()] and rim[J.min(), I.min() - 2] and rim[J.max() + 1, I.max()],
                        "the warp's reach covered")
        self.assertFalse((rim.astype(bool) & c.astype(bool)).any())

    def test_islands_filled(self):
        """An island of terrain cells inside a patch (not joined to the block's edge through terrain cells) is fine
        too, however big: its corners are all holes, and it drew pale whole (between a hairpin's legs)."""
        u = np.zeros((20, 20))
        raw = u.copy()
        for j in range(4, 15):                        # a ring of sharp spots round a 5 x 5 island
            for i in range(4, 15):
                if j in (4, 14) or i in (4, 14):
                    u[j, i] = 4.0
        c = np.asarray(fast._ef.fine_cells(u, raw, 1.5, 0.5, 1))
        self.assertTrue(c[6:13, 6:13].all(), "the island filled")
        self.assertFalse(c[0, :].any() or c[:, 0].any(), "the outside left alone")

    def test_rim_cover_over_the_terrain_either_split(self):
        """A rim cell's cover (fine_rim_mesh): its corners the terrain's samples lift up, its sides straight between
        them (the fine mesh's outline alike); on the upper hull (centre 0) nowhere under the terrain plus lift however
        it splits the cell, on finepatch.RIM_CENTRE's split exactly that plus lift."""
        u = np.array([[0.0, 3.0], [-1.0, 0.5]])                    # one cell, its diagonals' middles 0.25 and 1.0
        P, T = (np.asarray(a) for a in fast._ef.fine_rim_mesh(0.0, 0.0, 2.0, np.array([[0, 0]], np.int64), u, 0.01, 0))
        self.assertEqual(T.shape, (4, 3))
        corner = {(0.0, 0.0): 0.0, (2.0, 0.0): 3.0, (0.0, 2.0): -1.0, (2.0, 2.0): 0.5}
        for x, y, z in P:
            if (x, y) in corner:
                self.assertAlmostEqual(z, corner[(x, y)] + 0.01)
        rng = np.random.default_rng(3)
        for t in T:
            a, b, c = P[t]
            self.assertGreater((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]), 0.0, "up")
            for w in rng.dirichlet([1, 1, 1], 20):
                x, y, z = w @ P[t]
                fx, fy = x / 2.0, y / 2.0
                swne = (fx * 3.0 + fy * (0.5 - 3.0)) if fx >= fy else (fy * -1.0 + fx * (0.5 + 1.0))
                nwse = ((1 - fx - fy) * 0.0 + fx * 3.0 + fy * -1.0) if fx + fy <= 1 else \
                    ((1 - fy) * 3.0 + (1 - fx) * -1.0 + (fx + fy - 1) * 0.5)
                self.assertGreaterEqual(z, max(swne, nwse) + 0.01 - 1e-9)
        P, T = (np.asarray(a) for a in fast._ef.fine_rim_mesh(0.0, 0.0, 2.0, np.array([[0, 0]], np.int64), u, 0.01,
                                                               finepatch.RIM_CENTRE))
        for t in T:
            for w in rng.dirichlet([1, 1, 1], 20):
                x, y, z = w @ P[t]
                fx, fy = x / 2.0, y / 2.0
                nwse = ((1 - fx - fy) * 0.0 + fx * 3.0 + fy * -1.0) if fx + fy <= 1 else \
                    ((1 - fy) * 3.0 + (1 - fx) * -1.0 + (fx + fy - 1) * 0.5)
                self.assertAlmostEqual(z, nwse + 0.01, places=9)


class BlockFineTest(unittest.TestCase):
    def test_pieces_written_and_holes(self):
        import geodesy
        import heightmap
        frame = geodesy.EnuFrame(46.53, 10.45)
        size = 64
        xs, ys = heightmap.sample_coords(0.0, 0.0, size)

        def enu(la, lo):
            e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return np.asarray(e), np.asarray(n)

        def gen(la, lo):        # a road cut 4 m deep into a slope along x = 0
            e, _n = enu(la, lo)
            return 100.0 + np.where(e > 3.0, 4.0 + 0.2 * e, 0.0)

        def raw(la, lo):
            e, _n = enu(la, lo)
            return 100.0 + 0.6 * e
        u = heightmap.surface_u(frame, gen, xs, ys)
        with tempfile.TemporaryDirectory() as d:
            holes, fine = finepatch.block_fine(frame, gen, raw, {"u": u}, 0.0, 0.0, size, d, "b_fine", 1)
            self.assertIsNotNone(fine)
            self.assertTrue(holes.any())
            self.assertTrue(fine["parts"])
            import cdae
            for p in fine["parts"]:
                self.assertTrue(os.path.getsize(os.path.join(d, p["shape"])) > 0)
                # (its collision in meshes of COL_CHUNK_M beside the drawn one: Windows' collision build per mesh)
                self.assertEqual(p.get("collision"), "mesh")
                s = cdae.read(os.path.join(d, p["shape"]))
                cols = [n for n in s["names"] if n.startswith("Colmesh")]
                self.assertTrue(cols, "collision meshes")
                for m in s["meshes"][3::2]:      # (drawn, null; then each collision object's null and its own)
                    v = cdae.mesh_arrays(m)["verts"]
                    span = v.max(0) - v.min(0)
                    self.assertLessEqual(max(span[0], span[1]), finepatch.COL_CHUNK_M + 2 * 2.0 + 1e-6)
            b = finepatch.bank(0.0, 0.0, size)
            name = finepatch.material_name(b, __import__('surface').DIRT)
            self.assertEqual(fine["materials"], [[name, "beamngEarthDirt"]])
        flat = heightmap.surface_u(frame, raw, xs, ys)
        with tempfile.TemporaryDirectory() as d:
            holes, fine = finepatch.block_fine(frame, raw, raw, {"u": flat}, 0.0, 0.0, size, d, "b_fine", 1)
            self.assertIsNone(fine, "natural ground: nothing fine")


class FineTinTest(unittest.TestCase):
    """earthfast.fine_tin: the fine cells as an adaptive triangulation (the user, 2026-11-01: "the finer mesh should be
    adaptive ... not be force to allign to a grid"): it covers them exactly once, its pieces meet without cracks, its
    outline stays the terrain's edges, and a road's edge across the grid is drawn straight within its tolerance."""

    def test_road_edge_across_the_grid(self):
        n, sq = 40, 2.0

        def f(x, y):        # a road on a 10 % grade, its side falling 1:1.5 over 3 m, its edge 30 degrees off the grid
            d = x * np.sin(0.52) - y * np.cos(0.52) + 20.0
            s = np.clip(d / 3.0, 0.0, 1.0)
            return 0.1 * x - 4.5 * s * s * (3.0 - 2.0 * s)
        ys, xs = np.mgrid[0:n + 1, 0:n + 1] * sq
        u = f(xs, ys)
        mask = np.ones((n, n), np.uint8)
        mask[:, :2] = 0
        mask[:2, :] = 0
        J, I = np.nonzero(mask)
        ji = np.ascontiguousarray(np.c_[J, I], np.int64)
        k = finepatch.TIN_K
        pts = np.asarray(fast._ef.fine_points(0.0, 0.0, sq, k, ji))
        z = np.ascontiguousarray(f(pts[:, 0], pts[:, 1]).reshape(len(ji), (k + 1) ** 2))
        P, T, tc, tp = (np.asarray(a) for a in fast._ef.fine_tin(0.0, 0.0, sq, ji, z, u, mask, 16, 0.02, 16384))
        a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
        area = 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))
        self.assertGreater(float(area.min()), 0.0, "every triangle up")
        self.assertAlmostEqual(float(area.sum()), float(mask.sum()) * sq * sq, places=6)
        self.assertGreater(len(np.unique(tp)), 4, "in pieces")
        self.assertLess(len(T), len(ji) * 2 * finepatch.K ** 2, "fewer triangles than the 1 m grid")
        # every edge twice (by position, across the pieces too) but the outline's, which are the terrain's whole edges
        key = lambda i: (round(float(P[i, 0]), 6), round(float(P[i, 1]), 6))  # noqa: E731
        edges = {}
        for t in T:
            for m in range(3):
                e = tuple(sorted((key(t[m]), key(t[(m + 1) % 3]))))
                edges[e] = edges.get(e, 0) + 1
        self.assertFalse([e for e, m in edges.items() if m > 2])
        for (p0, p1), m in edges.items():
            if m == 1:
                self.assertTrue(p0[0] in (4.0, 80.0) and p1[0] == p0[0] or p0[1] in (4.0, 80.0) and p1[1] == p0[1])
                self.assertAlmostEqual(abs(p1[0] - p0[0]) + abs(p1[1] - p0[1]), sq, msg="a terrain edge, unsplit")
        # within a few cm of the ground away from the outline (which keeps the terrain's straight edges)
        rng = np.random.default_rng(1)
        err = []
        for t in T:
            q = rng.dirichlet([1, 1, 1], 4) @ P[t]
            ok = (q[:, 0] > 8.0) & (q[:, 1] > 8.0) & (q[:, 0] < 76.0) & (q[:, 1] < 76.0)
            err.extend(np.abs(q[ok, 2] - f(q[ok, 0], q[ok, 1])))
        self.assertLess(float(np.percentile(err, 99)), 0.05)
        self.assertLess(float(np.max(err)), 0.20)


class FineFilterTest(unittest.TestCase):
    def test_slivers_levelled_shapes_kept(self):
        """earthfast.fine_filter: a fin 0.75 m wide standing 1.4 m over a flat strip (grading's sliver between two
        cores: spikes in the fine mesh, 2026-11-01) is levelled; a flat road, a 1:1 slope, its crest and a 3 m wall's
        step come through as they are."""
        k, sq = 8, 2.0
        J, I = np.mgrid[0:20, 0:20]
        ji = np.ascontiguousarray(np.c_[J.ravel(), I.ravel()], np.int64)
        pts = np.asarray(fast._ef.fine_points(0.0, 0.0, sq, k, ji))
        x = pts[:, 0]

        def ground(x):
            z = np.where(x < 10.0, 0.0, np.where(x < 16.0, x - 10.0, 6.0))         # road, slope, crest, top
            z = np.where(x >= 30.0, 9.0, z)                                      # a wall's step
            return z
        fin = (x > 4.0) & (x < 4.75)
        # (the block's samples round the cells: a margin of 3 cells, the patch at cells 3..22)
        gx = np.arange(26) * sq - 3 * sq
        U = np.ascontiguousarray(np.tile(ground(gx), (26, 1)))
        U2 = np.ascontiguousarray(np.tile(1.5 * gx, (26, 1)))
        ji = ji + 3
        pts = pts + 3 * sq
        z = ground(x) + np.where(fin, 1.4, 0.0)
        out = np.asarray(fast._ef.fine_filter(ji, np.ascontiguousarray(z.reshape(len(ji), -1)), 3, U)).ravel()
        self.assertLess(float(np.abs(out[fin]).max()), 0.01, "the fin levelled")
        self.assertLess(float(np.abs(out - ground(x))[~fin].max()), 1e-3, "the rest as it was, by the edge too")
        self.assertLess(float(np.abs(out[fin]).max()), 0.01)
        # (a steep slope to the patch's edge: left as it is there, not raised by a window cut short)
        zs = np.ascontiguousarray((1.5 * x).reshape(len(ji), -1))
        out = np.asarray(fast._ef.fine_filter(ji, zs, 3, U2)).ravel()
        self.assertLess(float(np.abs(out - 1.5 * x).max()), 1e-3)


if __name__ == "__main__":
    unittest.main()


class BankTest(unittest.TestCase):
    def test_neighbours_never_share_a_bank(self):
        for size, g in ((512, 128.0), (1024, 256.0)):
            b = finepatch.bank(0.0, 0.0, size)
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-2, -1, 0, 1, 2):
                    if dx or dy:
                        self.assertNotEqual(finepatch.bank(dx * g, dy * g, size), b)
