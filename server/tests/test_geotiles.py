"""Origin-independent world gen (geotiles, .claude/work/geotiles_plan.md phases 1a / 1b):

- the grid and its maths (earthfast geotiles.rs): keys partition the Earth, quads, tiles near a point, world_map exact,
  placement exact at its pivot and within a fraction of a mm round it; the mod's geotile.lua agrees (keys and corners
  bit for bit, placements to 1e-9 m);
- grading plans per planning region: two anchors 15 km apart grade the same place to the same height (1e-6 m), the
  plan cache hits across anchors (no lat0 / lon0 in its keys);
- bridge / tunnel chains per region (roads.ChainStore): one deck profile per chain, whatever the anchor;
- vegetation tiles in tile frames (vegetation.geo_tile_items): the same items from any anchor once placed, the
  tracker keeps what it sent across a rebase.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import plancache  # noqa: E402
import roads  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import features, FRAME, LAT, LON  # noqa: E402
from test_approaches import overpass, flat_elev  # noqa: E402

MOD = Path(__file__).resolve().parents[2] / "mod"
LUAJIT = shutil.which("luajit")


def east(lat, lon, km):
    """The frame km east of (lat, lon)."""
    return geodesy.EnuFrame(lat, lon + km * 1000.0 / (111320.0 * math.cos(math.radians(lat))))


class GridTest(unittest.TestCase):
    def test_keys_partition_and_centres(self):
        x, y = geotiles.tile_of(LAT, LON)
        la, lo, s, w, n, e = geotiles.info(x, y)
        self.assertTrue(s <= LAT < n and w <= LON < e)
        self.assertTrue(s < la < n and w < lo < e)
        self.assertEqual(geotiles.parse(geotiles.key(x, y)), (17, x, y))
        # nested in its map cell
        self.assertEqual(geotiles.tile_cell(x, y), tuple(mapcells.cell_of(LAT, LON)))
        xs, ys = geotiles.tile_of(np.array([s + 1e-9, n - 1e-9]), np.array([w + 1e-9, e - 1e-9]))
        self.assertEqual((list(xs), list(ys)), ([x, x], [y, y]))

    def test_tiles_near_by_quad(self):
        a = east(LAT, LON, 15.0)
        p = a.geodetic_to_enu(LAT, LON, 0.0)
        got = geotiles.tiles_near(a, float(p[0]), float(p[1]), 600.0)
        self.assertEqual(tuple(got[0][:2]), geotiles.tile_of(LAT, LON))
        self.assertEqual(got[0][2], 0.0)
        self.assertTrue(all(d <= 600.0 for _x, _y, d in got))
        self.assertEqual([d for *_k, d in got], sorted(d for *_k, d in got))
        q = geotiles.quad(a, *geotiles.tile_of(LAT, LON))
        side = np.hypot(*(q[1] - q[0]))
        self.assertTrue(200 < side < 260, side)      # (~237 m at 39 deg)

    def test_world_map_round_trip_and_placement(self):
        t = geotiles.frame(*geotiles.tile_of(LAT, LON))
        a = east(LAT, LON, 15.0)
        pts = np.array([[100.0, -80.0, 1700.0], [-120.0, 110.0, 1550.0], [0.0, 0.0, 1650.0], [5.0, 7.0, -3.0]])
        there = geotiles.world_map(t, a, pts)
        back = geotiles.world_map(a, t, there)
        np.testing.assert_allclose(back, pts, atol=1e-7)
        la, lo, h = geotiles.world_to_geodetic(t, pts)
        x, y, z = geotiles.geodetic_to_world(t, la, lo, h)
        np.testing.assert_allclose(np.c_[x, y, z], pts, atol=1e-7)
        placed = geotiles.place(t, a, (0.0, 0.0, 1650.0), pts)
        self.assertLess(float(np.abs(placed - there).max()), 5e-4, "rigid + shear within half a mm over the tile")
        rigid = geotiles.place(t, a, (0.0, 0.0, 1650.0), pts) - np.outer(pts[:, 2] - 1650.0,
                                                                            geotiles.placement(t, a, (0, 0, 1650.0))[2])
        self.assertGreater(float(np.abs(rigid - there).max()), 0.1, "the shear matters: 23 cm per 100 m at 15 km")

    def test_world_frame_samples_its_own_vertices(self):
        f = geotiles.region_frame(*geotiles.region_of(LAT, LON))
        lat, lon = LAT + 0.1, LON + 0.15          # ~15 km from the region's centre
        x, y, _ = f.geodetic_to_enu(lat, lon, 0.0)
        la, lo, _ = f.enu_to_geodetic(x, y, 0.0)
        self.assertLess(abs(float(la) - lat) + abs(float(lo) - lon), 1e-11)


LUA_AGREE = r"""
package.path = '%s/beamng_earth/lua/ge/extensions/?.lua;' .. package.path
local g = require('beamngEarth/geodesy')
local gt = require('beamngEarth/geotile')
local inp = %s
local out = { keys = {}, tiles = {}, quads = {}, places = {} }
local function f(v) return string.format('%%.17g', v) end
for _, p in ipairs(inp.points) do
  local x, y = gt.tileOf(p[1], p[2], p[3])
  out.keys[#out.keys + 1] = { x, y }
end
for _, t in ipairs(inp.tiles) do
  local cl, cn = gt.centre(t[1], t[2], t[3])
  local s, w, n, e = gt.bounds(t[1], t[2], t[3])
  out.tiles[#out.tiles + 1] = { f(cl), f(cn), f(s), f(w), f(n), f(e) }
end
for _, q in ipairs(inp.quads) do
  local a = g.newAnchor(q[1], q[2])
  local c = gt.quad(a, q[3], q[4], q[5])
  out.quads[#out.quads + 1] = { f(c[1][1]), f(c[1][2]), f(c[2][1]), f(c[2][2]), f(c[3][1]), f(c[3][2]), f(c[4][1]),
    f(c[4][2]) }
end
for _, q in ipairs(inp.places) do
  local pl = gt.placement(g.newAnchor(q[1], q[2]), g.newAnchor(q[3], q[4]), q[5], q[6], q[7])
  local r = {}
  for i = 1, 9 do r[i] = f(pl.r[i]) end
  out.places[#out.places + 1] = { r = r, t = { f(pl.t[1]), f(pl.t[2]), f(pl.t[3]) },
    c = { f(pl.c[1]), f(pl.c[2]), f(pl.c[3]) } }
end
local function enc(v)
  if type(v) == 'table' then
    local parts = {}
    if #v > 0 then
      for _, x in ipairs(v) do parts[#parts + 1] = enc(x) end
      return '[' .. table.concat(parts, ',') .. ']'
    end
    for k, x in pairs(v) do parts[#parts + 1] = '"' .. k .. '":' .. enc(x) end
    return '{' .. table.concat(parts, ',') .. '}'
  end
  return tostring(v)
end
print(enc(out))
"""


@unittest.skipUnless(LUAJIT, "luajit")
class LuaAgreementTest(unittest.TestCase):
    """geotile.lua against earthfast's geotiles.rs: keys, centres, bounds and quad corners bit for bit; placements to
    1e-9 (Lua's geodesy iterates its latitudes, the Rust is Heikkinen's closed form)."""

    def test_agrees(self):
        rng = np.random.default_rng(7)
        points = [[float(a), float(b), 17] for a, b in zip(rng.uniform(-84, 84, 300), rng.uniform(-180, 180, 300))]
        points += [[LAT, LON, 17], [85.2, 10.0, 17], [-85.2, -10.0, 17], [0.0, 0.0, 17], [46.529, 10.4557, 10],
                   [34.0617, -118.2468, 12]]
        tiles = []
        for la, lo, z in points[:40] + points[-6:]:
            x, y = geotiles.tile_of(la, lo, z)
            tiles.append([z, x, y])
        quads = [[LAT, LON, 17, *geotiles.tile_of(LAT, LON)],
                 [LAT, LON + 0.17, 17, *geotiles.tile_of(LAT + 0.01, LON)],
                 [46.529, 10.4557, 17, *geotiles.tile_of(46.6, 10.3)]]
        places = []
        for z, x, y in tiles[:12]:
            cl, cn = geotiles.centre(x, y, z)
            places.append([cl, cn, cl + 0.07, cn - 0.11, 0.0, 0.0, 1650.0])
        inp = {"points": points, "tiles": tiles, "quads": quads, "places": places}

        def lua_table(v):
            if isinstance(v, list):
                return "{" + ",".join(lua_table(x) for x in v) + "}"
            if isinstance(v, dict):
                return "{" + ",".join(f"{k}={lua_table(x)}" for k, x in v.items()) + "}"
            return repr(float(v)) if isinstance(v, float) else str(v)
        script = LUA_AGREE % (MOD, lua_table(inp))
        out = subprocess.run([LUAJIT, "-"], input=script, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        want_keys = [list(geotiles.tile_of(la, lo, z)) for la, lo, z in points]
        self.assertEqual(got["keys"], want_keys, "keys bit for bit")
        for (z, x, y), lt in zip(tiles, got["tiles"]):
            self.assertEqual(lt, list(geotiles.info(x, y, z)), f"tile {z}/{x}/{y} centre and bounds bit for bit")
        for q, lq in zip(quads, got["quads"]):
            rq = geotiles.quad(geodesy.EnuFrame(q[0], q[1]), q[3], q[4], q[2]).ravel().tolist()
            self.assertEqual(lq, rq, "quad corners bit for bit")
        for q, lp in zip(places, got["places"]):
            r, t, c = geotiles.placement(geodesy.EnuFrame(q[0], q[1]), geodesy.EnuFrame(q[2], q[3]), q[4:7])
            np.testing.assert_allclose(lp["r"], r.ravel(), atol=1e-12)
            np.testing.assert_allclose(lp["t"], t, atol=1e-8)
            np.testing.assert_allclose(lp["c"], c, atol=1e-9)


class PlansAcrossAnchorsTest(unittest.TestCase):
    """Two anchors 15 km apart: the same heights at the same lat / lon."""

    def setUp(self):
        self.f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)], [(-200, -150), (250, 180)]],
                          ["residential", "secondary", "tertiary"])

    @staticmethod
    def hill(la, lo):
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        return 40.0 + 2000.0 * (la - LAT) + 900.0 * np.sin((lo - LON) * 4000.0)

    def points(self):
        e = np.linspace(-250.0, 250.0, 101)
        n = 30.0 * np.sin(e / 40.0) + 18.0
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.0)
        return np.asarray(la), np.asarray(lo)

    def test_same_heights_from_anchors_15_km_apart(self):
        la, lo = self.points()
        base = self.hill(la, lo)
        a = grading.Grader(FRAME, FakeMD(self.f), self.hill)
        b = grading.Grader(east(LAT, LON, 15.0), FakeMD(self.f), self.hill)
        za = a.apply(la, lo, base, grading.ROAD_LIFT)
        zb = b.apply(la, lo, base, grading.ROAD_LIFT)
        self.assertGreater(float(np.abs(za - base).max()), 0.05, "graded at all")
        self.assertLess(float(np.abs(za - zb).max()), 1e-6)

    def test_one_store_plans_once_for_every_anchor(self):
        md = FakeMD(self.f)
        store = grading.PlanStore(md, self.hill)
        la, lo = self.points()
        a = grading.Grader(FRAME, md, self.hill, store=store)
        a.apply(la, lo, self.hill(la, lo))
        calls = md.calls
        b = grading.Grader(east(LAT, LON, 15.0), md, self.hill, store=store)
        b.apply(la, lo, self.hill(la, lo))
        self.assertEqual(md.calls, calls, "the second anchor planned nothing again")
        # plans handed out in each anchor's own world: the same places
        pa, pb = a.plans(la, lo), b.plans(la, lo)
        self.assertEqual(len(pa), len(pb))
        for p, q in zip(pa, pb):
            ga = np.c_[FRAME.enu_to_geodetic(p.xy[:, 0], p.xy[:, 1], 0.0)[:2]]
            fb = b.frame
            back = geotiles.world_xy_map(fb, FRAME, q.xy)
            self.assertLess(float(np.abs(back - p.xy).max()), 1e-6)
            self.assertTrue(np.array_equal(p.z, q.z))
            del ga

    def test_plan_cache_hits_across_anchors(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.environ[plancache.RUN_ENV] = "geo"
        self.addCleanup(os.environ.pop, plancache.RUN_ENV, None)
        la, lo = self.points()
        a = grading.Grader(FRAME, FakeMD(self.f), self.hill)
        a.shared = plancache.for_grader(root, a, None, None)
        za = a.apply(la, lo, self.hill(la, lo))
        md = FakeMD(self.f)
        b = grading.Grader(east(LAT, LON, 15.0), md, self.hill)          # (another process, another anchor)
        b.shared = plancache.for_grader(root, b, None, None)
        zb = b.apply(la, lo, self.hill(la, lo))
        self.assertEqual(md.calls, 0, "read from the cache: no road data fetched")
        self.assertGreater(b.shared.stats["read"], 0)
        self.assertTrue(np.array_equal(za, zb))

    def test_region_border_neighbours_read_in(self):
        # a place whose cells straddle two planning regions: the same heights either side, from each region's own
        # planner or the neighbour's plans mapped in
        rx, ry = geotiles.region_of(LAT, LON)
        _c, _d, s, w, n, e = geotiles.info(rx, ry, geotiles.PLAN_Z)
        lat0, lon0 = (s + n) / 2, e         # the region's east edge
        f0 = geodesy.EnuFrame(lat0, lon0)
        M_LON = 111320.0 * math.cos(math.radians(lat0))
        ft = features([[(-300, 0), (300, 0)]], ["secondary"])
        ft.lonlat[:, 0] = ft.lonlat[:, 0] - LON + lon0
        ft.lonlat[:, 1] = ft.lonlat[:, 1] - LAT + lat0
        ee = np.linspace(-120, 120, 49)
        la, lo, _ = f0.enu_to_geodetic(ee, np.full(len(ee), 3.0), 0.0)
        la, lo = np.asarray(la), np.asarray(lo)
        ramp = lambda a_, o_: 10.0 + 0.02 * (np.asarray(o_) - lon0) * M_LON      # noqa: E731
        g = grading.Grader(f0, FakeMD(ft), ramp)
        z = g.apply(la, lo, ramp(la, lo))
        self.assertEqual(len(g.store.planners), 2, "both regions")
        # (the cells' boxes are axis-aligned in a frame whose north turns ~0.16 deg from the meridian at a region's
        # edge: the two sides' pieces overlap a few m and blend there, as cells far from one anchor always did)
        self.assertLess(float(np.abs(np.diff(z, 2)).max()), 0.03, "no step at the border")
        one = grading.Grader(f0, FakeMD(ft), ramp)
        one.store.frame_of = lambda region: f0          # (both cells in one frame)
        self.assertLess(float(np.abs(one.apply(la, lo, ramp(la, lo)) - z).max()), 0.02)


class ChainStoreTest(unittest.TestCase):
    def test_one_deck_profile_per_chain(self):
        f = overpass()
        solves = []
        cls = type("Counted", (roads.BridgeProfiles,), {})
        base_solve = roads.BridgeProfiles._solve

        def counted(self, comp, pool, ground):
            solves.append(tuple(comp))
            return base_solve(self, comp, pool, ground)
        cls._solve = counted
        cs = roads.ChainStore(cls, lambda key: f, flat_elev)
        a = roads.ChainView(cs, FRAME)
        b = roads.ChainView(cs, east(LAT, LON, 15.0))
        pa = a.parts([f], (-100, -100, 100, 100))
        pb_box = b.frame.geodetic_to_enu(LAT, LON, 0.0)
        cx, cy = float(pb_box[0]), float(pb_box[1])
        pb = b.parts([f], (cx - 100, cy - 100, cx + 100, cy + 100))
        self.assertEqual(len(solves), 1, "solved once for both anchors")
        self.assertEqual(len(pa), 1)
        self.assertEqual(len(pb), 1)
        (_f, _s, xa, za, _l), (_g, _t, xb, zb, _m) = pa[0], pb[0]
        back = geotiles.world_map(b.frame, FRAME, np.c_[xb, zb])
        np.testing.assert_allclose(back[:, :2], xa, atol=1e-6)
        np.testing.assert_allclose(back[:, 2], za, atol=1e-6)
        # and the same deck as a profile solved in the anchor's own frame (the old per-anchor store) to a mm
        own = roads.BridgeProfiles(FRAME, lambda key: f, flat_elev).parts([f], (-100, -100, 100, 100))[0]
        np.testing.assert_allclose(np.interp(xa[:, 0], own[2][:, 0], own[3]), za, atol=2e-3)

    def test_chain_across_stores_solved_by_its_owner(self):
        f = overpass()
        cs = roads.ChainStore(roads.BridgeProfiles, lambda key: f, flat_elev)
        a = roads.ChainView(cs, FRAME)
        st = cs.store_at(LAT, LON)
        other = cs.store((st.region[0] + 1, st.region[1]))         # (a neighbouring region's store)
        ring = f.lonlat
        todo = [(("cross", 0), f, 0)]
        mine = other.parts_todo(todo)
        self.assertEqual(len(mine), 1)
        self.assertIn(("cross", 0), st.done, "solved by the chain's own store")
        z_own = a.parts([f], (-100, -100, 100, 100))[0][3]
        z_other = geotiles.world_map(other.frame, FRAME, np.c_[mine[0][2], mine[0][3]])[:, 2]
        np.testing.assert_allclose(z_other, z_own, atol=1e-6)
        del ring


class FakeVeg:
    """vegetation.Vegetation's cell(key): n hashed items over the cell."""

    def __init__(self, n=4000):
        import vegetation
        self.v = vegetation
        self.n = n
        self.calls = 0

    def cell(self, key):
        self.calls += 1
        w, s, e, n = mapcells.CellKey(*tuple(key)).bounds()
        rng = np.random.default_rng(abs(hash(tuple(key))) % 2 ** 32)
        k = self.n
        f32 = np.float32
        return self.v.Items(rng.uniform(w, e, k), rng.uniform(s, n, k), rng.uniform(3, 20, k).astype(f32),
                            np.ones(k, f32), np.zeros(k, np.uint8), np.zeros(k, np.uint8),
                            rng.uniform(0, 1, k).astype(f32), rng.uniform(0, 360, k).astype(f32), np.ones(k, f32))


class VegGeoTilesTest(unittest.TestCase):
    def test_tiles_partition_their_cell(self):
        import vegetation
        fv = FakeVeg()
        key = mapcells.cell_of(LAT, LON)
        gx0, gy0 = key.x * 8, key.y * 8
        total, pivots = 0, []
        for gx in range(gx0, gx0 + 8):
            for gy in range(gy0, gy0 + 8):
                rows, pivot = vegetation.geo_tile_items(gx, gy, fv, lambda k: True, lambda la, lo: np.full(np.shape(la), 50.0))
                total += len(rows)
                f = geotiles.frame(gx, gy)
                q = geotiles.quad(f, gx, gy)
                for r in rows:          # (in the tile's frame: inside its quad, at its ground)
                    self.assertEqual(geotiles.quad_dist(q, r[1], r[2]), 0.0)
                if rows:
                    pivots.append(pivot)
        self.assertEqual(total, fv.n, "every item in exactly one tile")
        self.assertTrue(all(abs(p - 50.0) < 0.1 for p in pivots), "pivot: the median ground")

    def test_placed_rows_match_the_anchor_tiles(self):
        # the same items, placed from the tile frame into an anchor 15 km off, as tile_items gives them there
        import vegetation
        fv = FakeVeg(600)
        gx, gy = geotiles.tile_of(LAT, LON)
        flat = lambda la, lo: np.full(np.shape(la), 1650.0)     # noqa: E731
        rows, pivot = vegetation.geo_tile_items(gx, gy, fv, lambda k: True, flat)
        a = east(LAT, LON, 15.0)
        pts = np.array([r[1:4] for r in rows], np.float64)
        placed = geotiles.place(geotiles.frame(gx, gy), a, (0.0, 0.0, pivot), pts)
        exact = geotiles.world_map(geotiles.frame(gx, gy), a, pts)
        self.assertLess(float(np.abs(placed - exact).max()), 0.02, "(rows rounded to 1 cm)")

    def test_tracker_keeps_tiles_across_a_rebase(self):
        """A scripted rebase: the geo tracker builds nothing again; the anchor tracker builds every tile again."""
        import vegetation
        from concurrent.futures import ThreadPoolExecutor, wait

        class Loop:
            def __init__(self):
                self.futs = []

            def run_in_executor(self, pool, fn, *args):
                f = pool.submit(fn, *args)
                self.futs.append(f)
                return f

        class Conn:
            def __init__(self):
                self.msgs, self.lock = [], threading.Lock()

            def send(self, m):
                with self.lock:
                    self.msgs.append(m)
        built = []

        def build_geo(gx, gy):
            built.append((gx, gy))
            return "/x/veg_g.json", 1, geotiles.centre(gx, gy), 0.0

        def build_anchor(aid, tx, ty):
            built.append((aid, tx, ty))
            return "/x/veg.json", 1

        def drive(tr, build, frames):
            loop, conn = Loop(), Conn()
            counts = []
            for aid, f in frames:
                n0 = len(built)
                x, y, _ = f.geodetic_to_enu(LAT, LON, 0.0)
                for _ in range(40):         # (poses until nothing more is wanted)
                    tr.on_pose(conn, aid, float(x), float(y), loop, build, frame=f)
                    wait(loop.futs)
                    if len(tr.building) == 0 and all(k in tr.sent for k in tr.car):
                        break
                counts.append(len(built) - n0)
            tr.close()
            return counts, conn.msgs
        a, b = east(LAT, LON, -15.0), geodesy.EnuFrame(LAT, LON)
        geo, msgs = drive(vegetation.Tracker(per_pose=16, geo=True), build_geo, [(1, a), (2, b)])
        built.clear()
        old, _ = drive(vegetation.Tracker(per_pose=16), build_anchor, [(1, a), (2, b)])
        self.assertGreater(geo[0], 50)
        self.assertLessEqual(geo[1], 3, "after the rebase: (almost) nothing built again")
        self.assertGreater(old[1], 50, "the anchor's tiles: all of them again")
        self.assertTrue(all(m["key"].startswith("g17_") and "frame" in m and "pivot" in m
                            for m in msgs if m["type"] == "veg_tile"))
        print(f"\nscripted rebase (15 km): tiles built before {geo[0]} / after {geo[1]} (geotiles), "
              f"{old[0]} / {old[1]} (anchor tiles)")


if __name__ == "__main__":
    unittest.main()
