"""Global far tiles (geotiles phase 3b, docs/V1_4_SPEC.md 3.7 M6 on the geotiles grid; farfield.select_global,
earthfast farglobal.rs): far levels from 3 up as Web-Mercator tiles made in their own frames, the anchor's quadtree
(L <= 2) in the refined finest ones, its nodes across that region's edge cut to each global tile (pieces).

  - the selection covers the radius once (global quads, pieces, the anchor's nodes);
  - the same global tiles from two anchors 15 km apart (and with the horizon cull: its Mercator table);
  - a global tile's content is the same whatever the anchor (shape and texture bytes);
  - seams: neighbouring global tiles of one zoom share their edge vertices exactly (place and height); a piece's
    cut vertices lie on its node's triangles, its cut is skirted, two pieces of a node meet on identical vertices;
  - placement: a global tile 80 and 150 km out lands within its error budget (|h - pivot| x D / R) of an independent
    WGS84 computation, and its own vertices are where that computation puts them in its frame;
  - a scripted rebase (in-process server, synthetic DEM and imagery) sends no level >= 3 tile again.
"""
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import revisit  # noqa: E402
import test_server  # noqa: E402
from netclient import FarState  # noqa: E402

LAT, LON = 35.46, -118.77
LAY = ff.Layout(64, 512, True)


class FakeDem:
    """Smooth synthetic terrain, zoom independent (so seams compare exactly)."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        return 900 + 350 * np.sin(lat * 90) * np.cos(lon * 70) + 40 * np.sin(lat * 900)

    def stats_snapshot(self):
        return {}


def west(km):
    return LON - km * 1000.0 / (111320.0 * math.cos(math.radians(LAT)))


# --- an independent WGS84 (not geotiles.rs / geodesy.py): ECEF and the ENU rotation, closed form --------------------
A_WGS, F_WGS = 6378137.0, 1 / 298.257223563
E2_WGS = F_WGS * (2 - F_WGS)


def ecef(lat, lon, h):
    p, l = np.radians(lat), np.radians(lon)
    n = A_WGS / np.sqrt(1 - E2_WGS * np.sin(p) ** 2)
    return np.stack([(n + h) * np.cos(p) * np.cos(l), (n + h) * np.cos(p) * np.sin(l),
                     (n * (1 - E2_WGS) + h) * np.sin(p)], -1)


def world(lat0, lon0, lat, lon, h):
    """The world convention in the frame at (lat0, lon0): x, y of the place at h = 0, z the up of it at h."""
    p, l = math.radians(lat0), math.radians(lon0)
    rot = np.array([[-math.sin(l), math.cos(l), 0],
                    [-math.sin(p) * math.cos(l), -math.sin(p) * math.sin(l), math.cos(p)],
                    [math.cos(p) * math.cos(l), math.cos(p) * math.sin(l), math.sin(p)]])
    o = ecef(lat0, lon0, 0.0)
    g = (ecef(lat, lon, 0.0) - o) @ rot.T
    u = (ecef(lat, lon, h) - o) @ rot.T
    return np.stack([g[..., 0], g[..., 1], u[..., 2]], -1)


def merc_ll(z, x, y, u, v):
    """lat / lon of Mercator position (u east, v north) in tile (z, x, y)."""
    w = 2.0 ** z
    lon = (x + u) / w * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * (y + 1 - v) / w))))
    return lat, lon


def inside_convex(poly, pts):
    """Points (n, 2) strictly inside a convex polygon (flat, counter-clockwise or not)."""
    q = np.asarray(poly, np.float64).reshape(-1, 2)
    s = None
    for k in range(len(q)):
        a, b = q[k], q[(k + 1) % len(q)]
        c = (b[0] - a[0]) * (pts[:, 1] - a[1]) - (b[1] - a[1]) * (pts[:, 0] - a[0])
        s = c if s is None else s
        if k == 0:
            sign = np.sign(c)
        s = np.where(np.sign(c) == sign, s, 0)
    return s != 0


def footprint(t):
    return ff._square(t.bbox) if isinstance(t, ff.Tile) else t.poly


class SelectionTest(unittest.TestCase):
    def select(self, alat, alon, horizon=None, radius=60000.0, disc=False, max_level=9):
        fr = geodesy.EnuFrame(alat, alon)
        px, py, _ = fr.geodetic_to_enu(LAT, LON, 0.0)
        return fr, (float(px), float(py)), ff.select_global(
            fr, float(px), float(py), ff.global_zoom(alat), layout=LAY, max_level=max_level, min_level=-1,
            radius=radius, disc=disc, horizon=horizon, forced_level=-1)

    def test_covers_once(self):
        fr, (px, py), (out, _) = self.select(LAT, west(15))
        kinds = {type(t).__name__ for t in out.values()}
        self.assertEqual(kinds, {"Tile", "Piece", "GTile"})
        self.assertTrue(all(t.level >= 3 for t in out.values() if isinstance(t, ff.GTile)))
        self.assertTrue(all(t.level <= 2 for t in out.values() if not isinstance(t, ff.GTile)))
        rng = np.random.default_rng(3)
        pts = np.vstack([rng.uniform(-50000, 50000, (20000, 2)), rng.uniform(-6000, 6000, (20000, 2))])
        pts += (px, py)
        cnt = np.zeros(len(pts), int)
        edge = np.full(len(pts), np.inf)
        for t in out.values():
            p = np.asarray(footprint(t)).reshape(-1, 2)
            bb = t.bbox
            m = (pts[:, 0] > bb[0] - 50) & (pts[:, 0] < bb[2] + 50) & (pts[:, 1] > bb[1] - 50) & (pts[:, 1] < bb[3] + 50)
            idx = np.nonzero(m)[0]
            cnt[idx[inside_convex(p, pts[idx])]] += 1
            for k in range(len(p)):     # distance to the footprint's edges (the quads' straight edges stand off the
                a, b = p[k], p[(k + 1) % len(p)]    # curved parallels by up to ~L^2 tan(lat) / 8R)
                d = b - a
                tt = np.clip(((pts[idx] - a) @ d) / max(d @ d, 1e-9), 0, 1)
                edge[idx] = np.minimum(edge[idx], np.hypot(*(pts[idx] - (a + tt[:, None] * d)).T))
        bad = (cnt != 1) & (edge > 30.0)
        self.assertEqual(int(bad.sum()), 0, pts[bad][:5])

    def test_same_globals_from_two_anchors(self):
        _, _, (a, _) = self.select(LAT, west(15))
        _, _, (b, _) = self.select(LAT, LON)
        ga = {k for k in a if ff.is_global_key(k)}
        gb = {k for k in b if ff.is_global_key(k)}
        self.assertGreater(len(ga), 100)
        self.assertEqual(ga, gb)

    def test_same_globals_with_the_horizon_cull(self):
        dem = FakeDem()
        sets = []
        for alon in (west(15), LON):
            fr = geodesy.EnuFrame(LAT, alon)
            px, py, _ = fr.geodetic_to_enu(LAT, LON, 0.0)
            g = ff.horizon_grid(fr, dem, ff.horizon_extent(1000.0, 0), zoom=4)
            self.assertIsNotNone(g.merc)
            _, _, (out, _) = self.select(LAT, alon, horizon=ff.Horizon(1000.0, g), radius=200000.0, disc=True,
                                         max_level=ff.HORIZON_MAX_LEVEL)
            sets.append({k for k in out if ff.is_global_key(k)})
        self.assertEqual(sets[0], sets[1])

    def test_swap_groups_join_global_and_pieces(self):
        """A global tile refined into the anchor's nodes swaps with them in one group (same footprint)."""
        fr, (px, py), (a, ref) = self.select(LAT, LON)
        # the player 6 km further east: some global tiles split into pieces and nodes, some merge back
        _, _, (b, _) = self.select(LAT, LON + 0.07)
        b = {k: t for k, t in ff.select_global(fr, px + 6300.0, py, ff.global_zoom(LAT), prev_refined=ref, layout=LAY,
                                                max_level=9, min_level=-1, radius=60000.0,
                                                forced_level=-1)[0].items()}
        polys = {**{k: t.poly for k, t in a.items() if not isinstance(t, ff.Tile)},
                 **{k: t.poly for k, t in b.items() if not isinstance(t, ff.Tile)}}
        groups = ff.swap_groups(set(a), b, polys.get)
        two = [g for g in groups if g[0] and g[1]]
        self.assertTrue(any(any(ff.is_global_key(k) for k in g[1]) and any(not ff.is_global_key(k) for k in g[0])
                            for g in two), "a global tile replaced by the anchor's nodes in one group")
        for adds, removes in two:       # each side the same area (the quads' slivers aside)
            def area(keys, sel):
                tot = 0.0
                for k in keys:
                    p = np.asarray(footprint(sel[k])).reshape(-1, 2)
                    tot += 0.5 * abs(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1)))
                return tot
            aa, ar = area(adds, b), area(removes, a)
            self.assertLess(abs(aa - ar), 0.01 * max(aa, ar) + 1.0, (adds, removes))


class MeshTest(unittest.TestCase):
    def built(self, z, x, y):
        zoom = ff.global_dem_zoom(z, 64)
        return ff.build_global(FakeDem(), z, x, y, zoom, ff.global_dem_zoom(z + 1, 64), LAY)

    def geodetic(self, b, z, x, y):
        """(lat, lon, h) of every vertex of a global tile's Built (from its Mercator position; h from its world z)."""
        lat, lon = merc_ll(z, x, y, b.planar[:, 0], b.planar[:, 1])
        return lat, lon

    def test_vertices_where_wgs84_puts_them(self):
        z = 11
        x, y = geotiles.tile_of(LAT + 0.3, LON + 0.4, z)
        b = self.built(z, x, y)
        self.assertEqual(b.pos[:2], [0.0, 0.0])
        lat, lon = self.geodetic(b, z, x, y)
        cl, cn = ff.global_frame_ll(z, x, y)
        h = FakeDem().sample(lat, lon, 0)
        want = world(cl, cn, lat, lon, h)
        got = b.positions + b.pos
        ground = np.abs(got[:, 2] - want[:, 2]) < b.skirt / 2
        self.assertGreater(ground.sum(), 100)
        np.testing.assert_allclose(got[ground, :2], want[ground, :2], atol=1e-3)
        np.testing.assert_allclose(got[ground, 2], want[ground, 2], atol=b.err + 1e-3)

    def test_neighbours_share_their_edges(self):
        z = 13
        x, y = geotiles.tile_of(LAT, LON, z)
        a, e, s = self.built(z, x, y), self.built(z, x + 1, y), self.built(z, x, y + 1)

        def edge(b, tx, ty, axis, val):
            m = np.isclose(b.planar[:, axis], val) & (b.positions[:, 2] > b.positions[:, 2].min() - 1)
            lat, lon = merc_ll(z, tx, ty, b.planar[m, 0], b.planar[m, 1])
            cl, cn = ff.global_frame_ll(z, tx, ty)
            p = b.positions[m] + b.pos
            # (each vertex back to lat / lon / h in its own frame: the place both tiles share)
            la, lo, hh = geotiles.world_to_geodetic(geodesy.EnuFrame(cl, cn), p)
            keep = np.abs(hh - FakeDem().sample(la, lo, 0)) < 1.0       # (not the skirt copies)
            return {(round(float(u), 9), round(float(v), 9)): (float(q), float(r), float(t))
                    for u, v, q, r, t in zip(lat[keep], lon[keep], la[keep], lo[keep], hh[keep])}
        ea, ee = edge(a, x, y, 0, 1.0), edge(e, x + 1, y, 0, 0.0)
        shared = set(ea) & set(ee)
        self.assertGreater(len(shared), 3)
        for k in shared:
            np.testing.assert_allclose(ea[k], ee[k], atol=1e-6)     # same place, same height: watertight
        sa, ss = edge(a, x, y, 1, 0.0), edge(s, x, y + 1, 1, 1.0)
        shared = set(sa) & set(ss)
        self.assertGreater(len(shared), 3)
        for k in shared:
            np.testing.assert_allclose(sa[k], ss[k], atol=1e-6)

    def test_placement_within_budget(self):
        """The mod's placement (rigid at the pivot: geotile.lua) of a global tile 80 and 150 km out against an
        independent WGS84 world of the anchor: within |h - pivot| x D / R horizontally (D: to the tile's far corner;
        + relief x half diagonal / R + 2 cm), 5 cm vertically. Measured: 5.3 m at 80 km and 9.2 m at 150 km for 390 m
        of relief (the shear the mod leaves out would take it to 0.13 / 0.23 m): under a pixel there (~70 / 130 m)."""
        anchor = geodesy.EnuFrame(LAT, LON)
        for km in (80.0, 150.0):
            la = LAT + km * 1000.0 / 110950.0
            z = 11
            x, y = geotiles.tile_of(la, LON, z)
            b = self.built(z, x, y)
            cl, cn = ff.global_frame_ll(z, x, y)
            src = geodesy.EnuFrame(cl, cn)
            r, t, _c = geotiles.placement(src, anchor, b.pos)
            p = b.positions + b.pos
            got = p @ r.T + t                               # rigid (setPosRot): no shear
            lat, lon = merc_ll(z, x, y, b.planar[:, 0], b.planar[:, 1])
            h = FakeDem().sample(lat, lon, 0)
            ground = np.abs(p[:, 2] - world(cl, cn, lat, lon, h)[:, 2]) < 1.0
            want = world(LAT, LON, lat, lon, h)
            d = np.hypot(want[:, 0], want[:, 1])
            # |h - pivot| x D / R, D to the far corner, plus the relief x the tile's half diagonal / R (the move is
            # exact at the pivot only: the tile's own spread of up-directions)
            relief = float(np.ptp(p[ground, 2]))
            budget = (np.abs(p[:, 2] - b.pos[2]) * float(d.max()) + relief * b.size / math.sqrt(2)) / 6371000.0 + 0.02
            dxy = np.hypot(*(got[:, :2] - want[:, :2]).T)
            self.assertTrue(np.all(dxy[ground] <= budget[ground]), (km, float((dxy - budget)[ground].max())))
            self.assertLess(float(np.abs(got[ground, 2] - want[ground, 2]).max()), 0.05, km)
            self.assertTrue(70000 < float(d.min()) and float(d.max()) < 170000)

    def test_piece_cut(self):
        """A node cut by a quad: every ground vertex inside, the cut vertices on the node's own triangles (heights
        exact), skirts along the cut, and the two pieces of a node cut by a line meet on the same vertices."""
        fr = geodesy.EnuFrame(LAT, LON)
        node = ff.Tile(2, 3, 1)
        full = ff.build_tile(fr, FakeDem(), node, 13, ref_zoom=14, with_sink=False, layout=LAY)
        x0, y0, x1, y1 = node.bbox
        cx = x0 + 377.3                                     # a slanted cut through the node
        left = (x0 - 500, y0 - 500, cx + 40, y0 - 500, cx - 40, y1 + 500, x0 - 500, y1 + 500)
        right = (cx + 40, y0 - 500, x1 + 500, y0 - 500, x1 + 500, y1 + 500, cx - 40, y1 + 500)
        pa, pb = ff.clip_built(full, left), ff.clip_built(full, right)
        P = full.positions + full.pos

        def surface(xy):
            """Height of the node's mesh at plane points (its triangles, barycentric)."""
            out = np.full(len(xy), np.nan)
            tri = P[full.tris]
            flat = np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                          - (tri[:, 1, 1] - tri[:, 0, 1]) * (tri[:, 2, 0] - tri[:, 0, 0])) > 1e-9
            tri = tri[flat]
            for i, (qx, qy) in enumerate(xy):
                a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
                det = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
                w1 = ((qx - a[:, 0]) * (c[:, 1] - a[:, 1]) - (qy - a[:, 1]) * (c[:, 0] - a[:, 0])) / det
                w2 = ((b[:, 0] - a[:, 0]) * (qy - a[:, 1]) - (b[:, 1] - a[:, 1]) * (qx - a[:, 0])) / det
                w0 = 1 - w1 - w2
                k = np.nonzero((w0 > -1e-7) & (w1 > -1e-7) & (w2 > -1e-7))[0]
                if len(k):
                    j = k[0]
                    out[i] = w0[j] * a[j, 2] + w1[j] * b[j, 2] + w2[j] * c[j, 2]
            return out
        cut = {}
        for piece, poly in ((pa, left), (pb, right)):
            Q = piece.positions + piece.pos
            q = np.asarray(poly).reshape(-1, 2)
            a, b = q[1], q[2]                               # the cut edge of each polygon
            if piece is pb:
                a, b = q[3], q[0]
            dist = ((b[0] - a[0]) * (Q[:, 1] - a[1]) - (b[1] - a[1]) * (Q[:, 0] - a[0])) / np.hypot(*(b - a))
            on = np.abs(dist) < 1e-6
            top = Q[on & (Q[:, 2] > Q[:, 2].min() - 1e9)]
            s = surface(top[:, :2])
            ground = np.abs(top[:, 2] - s) < 1e-6
            self.assertGreater(int(ground.sum()), 10)
            lowered = np.abs(top[:, 2] - (s - full.skirt)) < 1e-6
            self.assertGreater(int(lowered.sum()), 10, "skirts along the cut")
            between = ~ground & ~lowered            # (the node's own edge skirts, cut: on its edge, under the ground)
            self.assertTrue(np.all((np.isclose(top[between, 1], y0) | np.isclose(top[between, 1], y1))
                                   & (top[between, 2] < s[between]) & (top[between, 2] > s[between] - full.skirt - 1e-6)))
            cut[id(piece)] = {(round(float(v[0]), 6), round(float(v[1]), 6), round(float(v[2]), 6))
                              for v in top[ground]}
        self.assertEqual(cut[id(pa)], cut[id(pb)], "the two pieces meet on the same vertices")


class ContentTest(unittest.TestCase):
    """make_tile of one global tile from two anchors: the same shape and texture bytes."""

    def test_same_bytes_from_two_anchors(self):
        on = revisit.ON
        revisit.ON = False
        self.addCleanup(setattr, revisit, "ON", on)
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None, near_visual="mesh", cells=64, block=512)
            s.dem = FakeDem()
            s.imagery = test_server.FakeImagery(os.path.join(tmp, "cache", "imagery"))
            try:
                z = 12
                x, y = geotiles.tile_of(LAT + 0.2, LON, z)
                out = []
                for alon in (west(15), LON):
                    a = s.new_anchor(LAT, alon)
                    q = tuple(float(v) for v in geotiles.quad(a.frame, x, y, z).ravel())
                    t = ff.GTile(z, x, y, 3 + a.zf - z, q, 1.0)
                    info = s.make_tile(a, t)
                    files = [os.path.join(s.out_dir, s.tile_name(earthd.GLOBAL_AID, t.key)),
                             os.path.join(s.out_dir, info.texture)]
                    data = []
                    for f in files:
                        with open(f, "rb") as fh:
                            data.append(fh.read())
                    out.append(data + [info.pos, info.frame])
                self.assertEqual(out[0], out[1])
                self.assertEqual(out[0][2][:2], [0.0, 0.0])
                self.assertEqual(tuple(out[0][3]), ff.global_frame_ll(z, x, y))
            finally:
                s.tile_pool.shutdown(wait=True)
                s.hm_pool.shutdown(wait=True)
                s.imagery.close()


class GlobalFarState(FarState):
    """FarState with global tiles: their key names no anchor, and they stay shown (or loading) across anchors."""

    current = None

    def switch(self, anchor_id):
        self.current = anchor_id
        self.held = {k: m for k, m in self.held.items() if k[0] in (anchor_id, 0)}

    def apply(self, msgs):
        for m in msgs:
            m = dict(m)
            if self.current is not None and m.get("anchorId") not in (None, self.current):
                # (a previous anchor's message that crossed the commit: the mod ignores its swaps and tiles, and a
                # far_remove of one of its tiles it dropped at the switch)
                if m["type"] in ("far_swap", "far_tile"):
                    continue
                if m["type"] == "far_remove" and (m["anchorId"], m["key"]) not in self.tiles:
                    continue
            if m["type"] in ("far_tile", "far_remove", "far_retex") and ff.is_global_key(m.get("key")):
                m["anchorId"] = 0
            if m["type"] == "far_swap":
                for side in ("add", "remove"):
                    for k in m[side]:
                        if ff.is_global_key(k):
                            ck = (0, k)
                            src = self.held if side == "add" else self.tiles
                            if side == "add":
                                self.tiles[ck] = src.pop(ck)
                            else:
                                del self.tiles[ck]
                m = dict(m, add=[k for k in m["add"] if not ff.is_global_key(k)],
                         remove=[k for k in m["remove"] if not ff.is_global_key(k)])
            super().apply([m])


class RebaseTest(test_server.ServerHarness):
    """A scripted rebase on the in-process server: the far field at a place from an anchor 15 km west, then from one
    at the place: no level >= 3 tile is sent again (global tiles stay), and what shows covers the radius."""
    near_visual = "mesh"
    cells = 64
    block = 512
    with_imagery = True

    def setUp(self):
        super().setUp()
        self.server.radius = 25000.0

    def hello_global(self, on=True):
        self.c.send({"type": "hello", "protocol": 4, "format": 4, "modVersion": "test", "geoTiles": geotiles.Z,
                     "globalFar": ff.GLOBAL_PROTO if on else 0})
        m, _ = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        return m

    def visit(self, far, req, alon, px, py):
        a = self.prepare_anchor(req, test_server.LAT, alon, cx=0.0, cy=0.0)
        aid = a["anchorId"]
        far.switch(aid)
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        self.c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0.0, "cy": 0.0})
        self.pose(aid, px, py)
        done, seen = self.stream_until_done(far, aid, timeout=300)
        return aid, done, [m for m in seen if m["type"] == "far_tile"]

    def test_rebase_sends_no_level3_tiles(self):
        m = self.hello_global()
        self.assertEqual(m.get("globalFar"), ff.GLOBAL_PROTO)
        far = GlobalFarState()
        lon_a = test_server.LON - 15000.0 / (111320.0 * math.cos(math.radians(test_server.LAT)))
        fa = geodesy.EnuFrame(test_server.LAT, lon_a)
        px, py, _ = fa.geodetic_to_enu(test_server.LAT, test_server.LON, 0.0)
        a1, _, first = self.visit(far, 1, lon_a, float(px), float(py))
        g1 = [m for m in first if ff.is_global_key(m["key"])]
        self.assertGreater(len(g1), 40)
        for m in g1:
            self.assertEqual(m["pos"][:2], [0, 0])
            self.assertEqual(len(m["frame"]), 2)
            self.assertEqual(len(m["poly"]), 8)
            self.assertGreaterEqual(m["level"], 3)
            self.assertTrue(os.path.exists(self.fs(m["shape"])) and os.path.exists(self.fs(m["texture"])))
        self.assertFalse([m for m in first if not ff.is_global_key(m["key"]) and m["level"] >= 3])
        self.assertTrue([m for m in first if ff.is_piece_key(m["key"])], "pieces along the region's edge")
        shown_g = {k for a, k in far.tiles if a == 0}
        a2, done, second = self.visit(far, 2, test_server.LON, 0.0, 0.0)
        again = [m["key"] for m in second if m["level"] >= 3]
        self.assertEqual(again, [], "no level >= 3 tile sent again after the rebase")
        self.assertEqual({k for a, k in far.tiles if a == 0}, shown_g)
        c = self.conn()
        self.assertEqual(done["count"], len(c.desired))
        self.assertTrue(all(c.ck(k) in c.shown for k in c.desired))

    def test_there_and_back_with_the_camera_away(self):
        """A rebase there and back (an anchor 1.4 km away and back to the first place, as at Pikes Peak, 2026-10-29)
        with a free camera 3 km from the car, the pose coming a while after each commit (as the game's): no level
        >= 3 tile sent again. (The camera's position stayed in the previous anchor's frame until the next pose: the
        far field was selected 1.4 km off at each commit, and global tiles went and came back.)"""
        import time
        self.hello_global()
        far = GlobalFarState()
        car = (test_server.LAT, test_server.LON)
        cam = (test_server.LAT + 0.004, test_server.LON + 0.03)       # ~3 km east of the car, a free camera
        anchors = [(test_server.LAT, test_server.LON), (test_server.LAT + 0.01, test_server.LON + 0.01),
                   (test_server.LAT, test_server.LON)]
        sent = []
        for req, (alat, alon) in enumerate(anchors, 1):
            fr = geodesy.EnuFrame(alat, alon)
            px, py, _ = fr.geodetic_to_enu(*car, 0.0)
            cx, cy, _ = fr.geodetic_to_enu(*cam, 0.0)
            px, py, cx, cy = (float(v) for v in (px, py, cx, cy))
            cxb, cyb = self.server.snap(px), self.server.snap(py)
            a = self.prepare_anchor(req, alat, alon, cx=cxb, cy=cyb)
            aid = a["anchorId"]
            far.switch(aid)
            self.c.send({"type": "commit_anchor", "anchorId": aid})
            self.c.send({"type": "commit_terrain", "anchorId": aid, "cx": cxb, "cy": cyb})
            time.sleep(1.0)                 # (the next pose: the game sends them every 0.5-1 s)
            self.c.send({"type": "pose", "anchorId": aid, "x": px, "y": py, "z": 0, "lat": car[0], "lon": car[1],
                         "speed": 0, "vx": 0, "vy": 0,
                         "cam": {"x": cx, "y": cy, "z": 200, "fx": 0, "fy": 1, "fz": 0, "fovDeg": 60}})
            _, seen = self.stream_until_done(far, aid, timeout=300)
            sent.append([m["key"] for m in seen if m["type"] == "far_tile" and m["level"] >= 3])
        self.assertGreater(len(sent[0]), 40)
        self.assertEqual(sent[1:], [[], []], "no level >= 3 tile sent again at either rebase")

    def test_rebase_while_global_tiles_load(self):
        """A rebase committed while the first far field still streams: the global tiles sent but not swapped in stay
        sent (the mod keeps them loading), none is sent twice (GlobalFarState asserts it), all show in the end."""
        self.hello_global()
        far = GlobalFarState()
        lon_a = test_server.LON - 15000.0 / (111320.0 * math.cos(math.radians(test_server.LAT)))
        fa = geodesy.EnuFrame(test_server.LAT, lon_a)
        px, py, _ = fa.geodetic_to_enu(test_server.LAT, test_server.LON, 0.0)
        a = self.prepare_anchor(1, test_server.LAT, lon_a, cx=0.0, cy=0.0)
        a1 = a["anchorId"]
        far.switch(a1)
        self.c.send({"type": "commit_anchor", "anchorId": a1})
        self.c.send({"type": "commit_terrain", "anchorId": a1, "cx": 0.0, "cy": 0.0})
        self.pose(a1, float(px), float(py))
        first, seen = self.c.until(lambda m: m["type"] == "far_tile" and ff.is_global_key(m["key"]), timeout=120)
        far.apply(seen + [first])
        self.c.send({"type": "prepare_anchor", "requestId": 2, "lat": test_server.LAT, "lon": test_server.LON,
                     "cx": 0.0, "cy": 0.0})
        b, more = self.c.until(lambda m: m["type"] == "anchor_ready", timeout=60)
        more.append(b)
        a2 = b["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": a2})
        self.c.send({"type": "commit_terrain", "anchorId": a2, "cx": 0.0, "cy": 0.0})
        self.pose(a2, 0.0, 0.0)
        _, rest = self.c.until(lambda m: m["type"] == "far_done" and m["anchorId"] == a2, timeout=300)
        msgs = more + rest              # in arrival order: the mod switches where the new anchor's messages start
        k = next(i for i, m in enumerate(msgs) if m.get("anchorId") == a2 and m["type"] != "anchor_ready")
        far.apply(msgs[:k])
        far.switch(a2)
        far.apply(msgs[k:])
        c = self.conn()
        shown = {k for a, k in far.tiles if a in (0, a2)}
        self.assertEqual(shown, set(c.desired))


if __name__ == "__main__":
    unittest.main()
