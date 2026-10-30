"""Road geotiles (phase 1c, .claude/work/geotiles_plan.md): a road tile's content built once in its geotile's frame, its
skin per anchor.

- ownership (geotiles.GeoBox.owns): every place in exactly one tile, whatever frame asks;
- seams (earthfast.clip_tris_convex): two neighbouring tiles' quads mapped into an anchor cut a mesh along the very same
  line: the pieces add up to the whole, none outside its quad; their shared corners are bit-equal;
- plan trimming by a cell's quad (earthfast.clip_segments_convex): a box gives clip_segments' answer, two cells split a
  road without a gap or an overlap;
- exact variants (earthfast.cdae_zstats / cdae_shear): a tall shape placed rigidly at its pivot is off by relief x D / R,
  its variant within a mm of the exact world map;
- the tracker (roads.GeoTracker): a scripted rebase (15 km) builds no content again, a skin for each tile round the car
  (31-46), exact variants only for the parts the rigid placement leaves off, drops the far tiles;
- with the map data cache (~/.cache/beamng-earth, Fells Point): the same geotile's content from two anchors 15 km apart
  is byte-identical; its skin's vertices lie on the anchor's drawn ground; the skins' seams.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import grading  # noqa: E402
import roads  # noqa: E402

LAT, LON = 39.2834, -76.5934
CACHE = os.path.expanduser("~/.cache/beamng-earth")
HAVE_DATA = os.path.isdir(os.path.join(CACHE, "mapdata")) and os.environ.get("BNGE_ROADGEO_REAL", "1") != "0"


def east(lat, lon, km):
    return geodesy.EnuFrame(lat, lon + km * 1000.0 / (111320.0 * math.cos(math.radians(lat))))


class OwnershipTest(unittest.TestCase):
    def test_every_place_in_one_tile(self):
        gx, gy = geotiles.tile_of(LAT, LON)
        a = east(LAT, LON, 15.0)
        rng = np.random.default_rng(3)
        # points round the tile's corners and edges in anchor a's world (h = 0)
        q = geotiles.quad(a, gx, gy)
        pts = np.concatenate([c + rng.normal(0.0, [0.02, 0.02], (400, 2)) for c in q] +
                             [rng.uniform(q.min(0) - 5, q.max(0) + 5, (2000, 2))])
        la, lo, _ = geotiles.world_to_geodetic(a, np.c_[pts, np.zeros(len(pts))])
        count = np.zeros(len(pts), int)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                b = geotiles.geobox(gx + i, gy + j)
                # the points in this tile's own frame (its world, exactly)
                own = geotiles.world_map(a, b.frame, np.c_[pts, np.zeros(len(pts))])[:, :2]
                o = b.owns(own[:, 0], own[:, 1])
                tx, ty = geotiles.tile_of(la, lo)
                self.assertTrue(np.array_equal(o, (tx == gx + i) & (ty == gy + j)), (i, j))
                count += o
        inner = (np.abs(pts - q.mean(0)) < 300).all(1)
        self.assertTrue((count[inner] == 1).all(), "every place in exactly one of the 9 tiles")

    def test_box_owns_anchor_tiles_half_open(self):
        box = roads.tile_bbox(1, 2)
        self.assertTrue(roads.box_owns(box, 256.0, 512.0))
        self.assertFalse(roads.box_owns(box, 512.0, 600.0))
        self.assertTrue(np.array_equal(roads.box_owns(box, np.array([256.0, 511.9, 512.0]), np.array([600.0] * 3)),
                                       [True, True, False]))


class SeamsTest(unittest.TestCase):
    def test_neighbour_quads_split_a_mesh_exactly(self):
        fast.require(104, "clip_tris_convex")
        a = east(LAT, LON, -15.0)
        gx, gy = geotiles.tile_of(LAT, LON)
        quads = {(i, j): geotiles.quad(a, gx + i, gy + j) for i in (0, 1) for j in (0, 1)}
        # bit-equal shared corners
        # (y grows south: (0, 1) is (0, 0)'s southern neighbour)
        self.assertTrue(np.array_equal(quads[(0, 0)][1], quads[(1, 0)][0]))
        self.assertTrue(np.array_equal(quads[(0, 0)][0], quads[(0, 1)][3]))
        self.assertTrue(np.array_equal(quads[(0, 0)][1], quads[(1, 1)][3]))
        # a random soup of triangles over the corner the four share
        c = quads[(0, 0)][1]
        rng = np.random.default_rng(5)
        P = np.c_[c + rng.uniform(-60, 60, (300, 2)), rng.uniform(0, 5, 300)]
        T = rng.integers(0, 300, (200, 3))
        T = T[(T[:, 0] != T[:, 1]) & (T[:, 1] != T[:, 2]) & (T[:, 0] != T[:, 2])]
        UV = P[:, :2] / 10.0

        def area(P2, T2):
            A, B, C = P2[T2[:, 0]], P2[T2[:, 1]], P2[T2[:, 2]]
            return float(np.abs((B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])).sum() / 2)
        total = 0.0
        union = np.array([quads[(0, 1)][0], quads[(1, 1)][1], quads[(1, 0)][2], quads[(0, 0)][3]])   # SW SE NE NW
        for k, q in quads.items():
            P2, UV2, T2, src = fast._ef.clip_tris_convex(np.ascontiguousarray(P), np.ascontiguousarray(UV),
                                                        np.ascontiguousarray(T, np.int64), np.ascontiguousarray(q))
            used = np.unique(T2)
            self.assertTrue(all(geotiles.quad_dist(q, float(x), float(y)) < 1e-9 for x, y in P2[used, :2]), k)
            total += area(P2, T2)
        # the union of the four quads (a convex quad too, to a mm): clipped once, the same area
        P3, _UV3, T3, _s = fast._ef.clip_tris_convex(np.ascontiguousarray(P), np.ascontiguousarray(UV),
                                                     np.ascontiguousarray(T, np.int64),
                                                     np.ascontiguousarray(union))
        self.assertLess(abs(total - area(P3, T3)), 1e-6 * max(1.0, total) + 0.05)

    def test_plan_trimmed_by_two_cells_quads(self):
        a = geotiles.region_frame(*geotiles.region_of(LAT, LON))
        import mapcells
        k1 = mapcells.cell_of(LAT, LON)
        k2 = mapcells.CellKey(k1.z, k1.x + 1, k1.y)

        class Pl:
            frame = a
        q1 = grading._Planner._clip_quad(Pl, k1)
        q2 = grading._Planner._clip_quad(Pl, k2)
        self.assertTrue(np.array_equal(q1[1], q2[0]) and np.array_equal(q1[2], q2[3]), "shared corners bit-equal")
        mid = 0.5 * (q1[1] + q1[2])
        xy = np.array([mid - [300.0, 7.0], mid + [5.0, 1.0], mid + [400.0, 9.0]])
        p = grading.Plan(xy, np.array([1.0, 2.0, 3.0]), 4.0)
        a1 = grading.trim_plan(p, q1)
        a2 = grading.trim_plan(p, q2)
        L = lambda ps: sum(float(np.hypot(*np.diff(q.xy, axis=0).T).sum()) for q in ps)    # noqa: E731
        whole = float(np.hypot(*np.diff(xy, axis=0).T).sum())
        inside = L(grading.trim_plan(p, np.array([q1[0], q2[1], q2[2], q1[3]])))
        self.assertAlmostEqual(L(a1) + L(a2), inside, places=6)
        self.assertGreater(whole, inside - 1e-9)
        # a box gives the box clip's answer
        box = (float(q1[:, 0].min()), float(q1[:, 1].min()), float(q1[:, 0].max()), float(q1[:, 1].max()))
        bq = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]])
        self.assertAlmostEqual(L(grading.trim_plan(p, box)), L(grading.trim_plan(p, bq)), places=6)


class ExactVariantTest(unittest.TestCase):
    def test_shear_lands_a_tall_shape_exactly(self):
        fast.require(104, "cdae_shear")
        gx, gy = geotiles.tile_of(LAT, LON)
        tf = geotiles.frame(gx, gy)
        a = east(LAT, LON, 15.0)
        # a 120 m tower on the ground at 40 m, 30 m off the tile's centre
        pos = np.array([30.0, -20.0, 0.0])
        P = np.array([[0, 0, 40], [5, 0, 40], [5, 5, 160], [0, 5, 160], [2, 2, 100]], np.float64)
        T = np.array([[0, 1, 2], [0, 2, 3], [1, 2, 4]])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.cdae")
            cdae.write(path, P, T)
            blob = open(path, "rb").read()
            n, med, lo, hi = fast._ef.cdae_zstats(blob)
            self.assertEqual(n, 5)
            self.assertAlmostEqual(med, 100.0, places=4)
            self.assertAlmostEqual(lo, 40.0, places=4)
            self.assertAlmostEqual(hi, 160.0, places=4)
            piv = pos[2] + med
            r, t, c = geotiles.placement(tf, a, (0.0, 0.0, piv))
            world = P + pos
            exact = geotiles.world_map(tf, a, world)
            rigid = world @ r.T + t
            self.assertGreater(float(np.abs(rigid - exact)[:, :2].max()), 0.1, "rigid: off by relief x D / R")
            s = r.T @ c
            var = fast._ef.cdae_shear(blob, float(pos[2]), float(s[0]), float(s[1]), float(s[2]), float(piv))
            d2 = cdae.decode(var)
            V = cdae.mesh_arrays(d2["meshes"][0])["verts"].astype(np.float64)
            # the engine's vertices (the triangle winding / order kept: match by the original order of P)
            V0 = cdae.mesh_arrays(cdae.decode(blob)["meshes"][0])["verts"].astype(np.float64)
            order = [int(np.argmin(np.abs(V0 - p).sum(1))) for p in P]
            placed = (V[order] + pos) @ r.T + t
            self.assertLess(float(np.abs(placed - exact).max()), 0.002)

    def test_auto_picks_the_tall_parts(self):
        gx, gy = geotiles.tile_of(LAT, LON)
        tf = geotiles.frame(gx, gy)
        parts = {"bld": {"pivot": 50.0, "relief": 60.0, "col": False},
                 "bldcol": {"pivot": 20.0, "relief": 20.0, "col": True},
                 "wire": {"pivot": 10.0, "relief": 1.0, "col": False}}
        self.assertEqual(roads.geo_exact_auto(tf, geodesy.EnuFrame(LAT, LON), parts), {})
        far = roads.geo_exact_auto(tf, east(LAT, LON, 15.0), parts)
        self.assertEqual(sorted(far), ["bld", "bldcol"])       # (60 x 2.35e-3 = 14 cm; 20 x 2.35e-3 = 4.7 cm)


class GeoTrackerRebaseTest(unittest.TestCase):
    """A scripted 15 km rebase (the mod's poses round one place, first from an anchor 15 km west, then from one at it):
    no content is built again, a skin for each tile round the car."""

    def test_rebase_sends_skins_only(self):
        from concurrent.futures import ThreadPoolExecutor, wait  # noqa: F401
        jobs = []
        lock = threading.Lock()

        class Loop:
            def __init__(self):
                self.futs = []

            def run_in_executor(self, pool, fn, *args):
                f = pool.submit(fn, *args)
                self.futs.append(f)
                return f

        class Conn:
            def __init__(self):
                self.msgs = []

            def send(self, m):
                with lock:
                    self.msgs.append(m)

        def build(aid, gx, gy, content, skin, exact):
            with lock:
                jobs.append((aid, gx, gy, content, skin, dict(exact) if isinstance(exact, dict) else exact))
            info = {"h": f"h{gx}_{gy}", "parts": {"bld": {"pivot": 30.0, "relief": 60.0, "col": False},
                                                  "bldcol": {"pivot": 15.0, "relief": 20.0, "col": True},
                                                  "wire": {"pivot": 9.0, "relief": 1.0, "col": False}}}
            ex = {}
            if exact == "auto":
                ex = roads.geo_exact_auto(geotiles.frame(gx, gy), frames[aid], info["parts"])
            elif exact:
                ex = dict(exact)
            return {"content": ("/x/rg.json", 1, info, []) if content else None,
                    "skin": ("/x/rs.json", {}) if skin else None,
                    "exact": {w: f"/x/rx_{aid}_{w}.cdae" for w in ex}, "time": {}}
        a, b = east(LAT, LON, -15.0), geodesy.EnuFrame(LAT, LON)
        frames = {1: a, 2: b}
        tr = roads.GeoTracker()
        loop, conn = Loop(), Conn()

        def drive(aid, f):
            x, y, _ = f.geodetic_to_enu(LAT, LON, 0.0)
            n0 = len(jobs)
            for _ in range(60):
                tr.on_pose(conn, aid, float(x), float(y), loop, build, frame=f)
                wait(loop.futs)
                if not tr.building and all(k in tr.sent and k in tr.skins for k in tr.car) and \
                        not any(tr.need.get((k, aid)) for k in tr.car):
                    break
            return jobs[n0:]
        first = drive(1, a)
        m0 = len(conn.msgs)
        second = drive(2, b)
        tr.close()
        msgs = conn.msgs[m0:]
        content_after = [j for j in second if j[3]]
        skins_after = [j for j in second if j[4]]
        self.assertGreater(len([j for j in first if j[3]]), 25)
        self.assertEqual(content_after, [], "after the rebase: no content built again")
        self.assertTrue(31 <= len(skins_after) <= 46, len(skins_after))
        self.assertFalse([m for m in msgs if m["type"] == "roads_tile"], "no content sent again")
        self.assertEqual(len([m for m in msgs if m["type"] == "roads_skin"]), len(skins_after))
        # the first anchor (15 km off): the tall parts came exact with their content; at the rebase they go back to
        # their base shapes (no build: roads_exact false)
        first_tiles = [m for m in conn.msgs[:m0] if m["type"] == "roads_tile"]
        self.assertTrue(all(set(m.get("exact", {})) == {"bld", "bldcol"} for m in first_tiles),
                        "at 15 km: the 60 m buildings (14 cm off) and their 20 m collision mesh (4.7 cm) exact")
        base = [m for m in msgs if m["type"] == "roads_exact"]
        self.assertTrue(base and all(set(m["parts"].values()) == {False} for m in base), "back to the base shapes")
        self.assertTrue(all(m["key"].startswith("g17_") and m.get("frame") for m in first_tiles))
        print(f"\nscripted rebase (15 km): content built before {len([j for j in first if j[3]])} / after "
              f"{len(content_after)}, skins after {len(skins_after)}, parts back to base {sum(len(m['parts']) for m in base)}")


@unittest.skipUnless(HAVE_DATA, "the map data cache (~/.cache/beamng-earth)")
class RealDataTest(unittest.TestCase):
    def test_content_identical_from_two_anchors(self):
        """The geotile under Fells Point built after a session's anchor tile there, once for an anchor at the place
        and once for one 15 km east: every content file byte for byte the same."""
        here = os.path.dirname(os.path.abspath(__file__))
        tmp = tempfile.mkdtemp(prefix="roadgeo_")
        try:
            lon_b = LON + 15000.0 / (111320.0 * math.cos(math.radians(LAT)))
            outs = []
            for i, (alat, alon) in enumerate(((LAT, LON), (LAT, lon_b))):
                r = subprocess.run([sys.executable, os.path.join(here, "roadgeo_content.py"), os.path.join(tmp, str(i)),
                                    str(LAT), str(LON), str(alat), str(alon)], capture_output=True, text=True,
                                   timeout=900)
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                outs.append(r.stdout)
            self.assertTrue(outs[0].strip(), "content written")
            self.assertEqual(outs[0], outs[1])
            self.assertIn(".skin.pkl", outs[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skin_on_the_drawn_ground_and_seams(self):
        import blockworker as bw
        import revisit
        import roadmesh
        revisit.ON = False
        bw._s = bw._State(CACHE, True, True, True, True, True, True, True)
        out = tempfile.mkdtemp(prefix="roadgeo_")
        try:
            f = geodesy.EnuFrame(LAT, LON)
            gx, gy = geotiles.tile_of(LAT, LON)
            keys = [(gx, gy), (gx + 1, gy)]
            q = 1 / 32
            g, _ = bw._s.anchor(1, LAT, LON)

            def elev(la, lo):
                z = bw.ground(f, la, lo, bw._s.dem.sample(la, lo, 15))
                return g.apply(la, lo, z, grading.ROAD_LIFT) if g is not None else z
            for k in keys:
                r = bw.roads_geo(1, LAT, LON, 15, k[0], k[1], 15, out, "/x", True, 0.0, True, False, True, q, True)
                data = json.load(open(os.path.join(out, os.path.basename(r["skin"][0]))))
                self.assertIn("road", data)
                d = cdae.read(os.path.join(out, os.path.basename(data["road"]["shape"])))
                m = next(m for m in d["meshes"] if m["type"] == 0)
                V = cdae.mesh_arrays(m)["verts"].astype(np.float64) + np.asarray(data["road"]["pos"])
                quad = geotiles.quad(f, *k)
                self.assertLess(max(geotiles.quad_dist(quad, float(x), float(y)) for x, y in V[:, :2]), 1e-4)
                gz = roadmesh.drawn_ground(f, elev, V[:, :2], q)
                dz = V[:, 2] - gz
                ok = np.isfinite(dz) & (np.abs(dz) < 0.3)
                lifts = np.array(sorted({round(float(v), 3) for v in roadmesh.LIFT.values()}))
                near = np.min(np.abs(dz[ok][:, None] - lifts[None, :]), axis=1)
                self.assertGreater(float((near <= 0.001).mean()), 0.9, "draped vertices on the drawn ground + lift")
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
