"""The generated ground (genground.py, earthfast genground.rs 306; BNE_GEN_GROUND): polygons filled at the texel
centres, the kind / region rules, the colours (a region's own colour at its border instead of the 10 m photo's smear,
flat water, nothing changed where no kind is known), world-locked detail (blocks agree along their seams), the same
bits on any number of threads; the imagery chain cut to Sentinel-2 with the switch on, and the switch in the keys of
what is kept."""
import importlib
import os
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import genground as gg  # noqa: E402
import imagery  # noqa: E402
import revisit  # noqa: E402

fast.require(gg.NEED, "generated ground")
EF = fast._ef
G = np.array([90, 130, 60], np.float64)     # a field's green
D = np.array([30, 45, 25], np.float64)      # a forest's dark green


def fill(h, w, polys):
    """gg_fill_polys of polys [(rings [(k, 2) texel (col, row)], value)]."""
    out = np.zeros((h, w), np.uint32)
    pts, ring_off, poly_off, vals = [], [0], [0], []
    for rings, v in polys:
        for r in rings:
            pts.append(np.asarray(r, np.float64))
            ring_off.append(ring_off[-1] + len(r))
        poly_off.append(len(ring_off) - 1)
        vals.append(v)
    n = EF.gg_fill_polys(out, np.concatenate(pts), np.asarray(ring_off, np.int64), np.asarray(poly_off, np.int64),
                         np.asarray(vals, np.uint32))
    return out, n


def square(c0, r0, c1, r1):
    return [(c0, r0), (c1, r0), (c1, r1), (c0, r1)]


def blurred_edge(n, col, a, b, width=10):
    """An n x n photo: a left of col, b right of it, ramped over `width` texels (the 10 m photo's smear)."""
    x = np.clip((np.arange(n) - (col - width / 2)) / width, 0.0, 1.0)[None, :, None]
    img = a[None, None, :] * (1 - x) + b[None, None, :] * x
    return np.ascontiguousarray(np.broadcast_to(img, (n, n, 3)).round().astype(np.uint8))


def gen(s2, kind, region=None, canopy=None, ox=1000.0, oy=2000.0, gr=None, gc=None, poly=None, dirs=None,
        phases=None):
    """gen_ground at 1 m texels; texel (0, 0) at world (ox, oy), on the global grid at (-oy, ox) by default."""
    n = kind.shape[0]
    region = kind.astype(np.uint32) if region is None else region
    canopy = np.full((n, n), 255, np.uint8) if canopy is None else canopy
    poly = np.zeros((n, n), np.uint32) if poly is None else poly
    dirs = np.zeros((0, 2)) if dirs is None else np.asarray(dirs, np.float64)
    phases = np.zeros(len(dirs)) if phases is None else np.asarray(phases, np.float64)
    return EF.gen_ground(np.ascontiguousarray(s2), np.ascontiguousarray(kind, np.uint8),
                         np.ascontiguousarray(region, np.uint32), np.ascontiguousarray(canopy, np.uint8),
                         np.ascontiguousarray(poly, np.uint32), np.ascontiguousarray(dirs),
                         np.ascontiguousarray(phases),
                         float(ox), float(oy), 1.0, int(-oy if gr is None else gr), int(ox if gc is None else gc))


class FillPolysTest(unittest.TestCase):
    def test_square_at_texel_centres(self):
        out, n = fill(10, 10, [([square(2, 2, 6, 6)], 7)])
        want = np.zeros((10, 10), np.uint32)
        want[2:6, 2:6] = 7
        np.testing.assert_array_equal(out, want)
        self.assertEqual(n, 16)

    def test_hole_and_later_over_earlier(self):
        out, _ = fill(12, 12, [([square(1, 1, 11, 11), square(4, 4, 8, 8)], 1), ([square(0, 0, 3, 3)], 2)])
        self.assertEqual(out[5, 5], 0, "the hole (even-odd)")
        self.assertEqual(out[2, 6], 1)
        self.assertEqual(out[1, 1], 2, "the later polygon over the earlier")
        self.assertEqual(out[11, 11], 0)

    def test_outside_and_bad_offsets(self):
        out, n = fill(8, 8, [([square(-20, -20, -10, -10)], 3)])
        self.assertEqual((n, int(out.sum())), (0, 0))
        with self.assertRaises(ValueError):
            EF.gg_fill_polys(np.zeros((4, 4), np.uint32), np.zeros((4, 2)), np.array([0, 9], np.int64),
                             np.array([0, 1], np.int64), np.array([1], np.uint32))


class RegionsTest(unittest.TestCase):
    def regions(self, base, poly=None, pkinds=(), canopy=None, cover=None, line=None, lkinds=()):
        base = np.asarray(base, np.uint8)
        sh = base.shape
        poly = np.zeros(sh, np.uint32) if poly is None else np.asarray(poly, np.uint32)
        line = np.zeros(sh, np.uint32) if line is None else np.asarray(line, np.uint32)
        canopy = np.full(sh, 255, np.uint8) if canopy is None else np.asarray(canopy, np.uint8)
        cover = np.zeros(sh, np.uint8) if cover is None else np.asarray(cover, np.uint8)
        return EF.gg_regions(base, poly, np.asarray(pkinds, np.uint8), line, np.asarray(lkinds, np.uint8), canopy,
                             cover)

    def test_polygon_rules(self):
        base = [[gg.TREE, gg.BUILT, gg.CROP, gg.WATER, gg.BUILT, gg.CROP, gg.BARE]]
        poly = [[1, 1, 1, 1, 2, 2, 2]]
        k, r = self.regions(base, poly, (gg.LAWN, gg.TREE))
        self.assertEqual(k.tolist(), [[gg.TREE, gg.LAWN, gg.LAWN, gg.WATER, gg.BUILT, gg.TREE, gg.BARE]],
                         "a park keeps its trees and water; a forest polygon keeps built-up and bare land")
        self.assertEqual(r[0, 1], (1 << 5) | gg.LAWN)
        self.assertEqual(r[0, 0], (1 << 5) | gg.TREE, "a park's trees: a region of their own")
        self.assertNotEqual(r[0, 5], r[0, 0], "each polygon's trees their own region")

    def test_canopy_and_cover(self):
        base = [[gg.GRASS, gg.GRASS, gg.TREE, gg.TREE, gg.WATER, gg.GRASS, gg.GRASS, gg.GRASS]]
        canopy = [[12, 0, 1, 0, 30, 255, 20, 20]]
        cover = [[0, 0, 0, 0, 0, 1, 2, 2 | 4]]
        k, r = self.regions(base, canopy=canopy, cover=cover)
        self.assertEqual(k.tolist(), [[gg.TREE, gg.GRASS, gg.SHRUB, gg.GRASS, gg.WATER, gg.WATER, gg.PAVED, gg.ROOF]])
        r = r[:, 1:]
        self.assertEqual(r[0, 5], gg.PAVED)
        self.assertEqual(r[0, 6], gg.ROOF)
        k2, r2 = self.regions([[gg.BUILT, gg.BUILT]], poly=[[1, 2]], pkinds=(gg.LAWN, gg.LAWN), cover=[[2, 2]])
        self.assertEqual(r2[0, 0], r2[0, 1], "roads: one region whatever the polygons under them")

    def test_hedgerows_grow(self):
        # (stage 2) the canopy model draws a hedgerow's tall core only: open ground with some canopy next to a tree
        k, _ = self.regions([[gg.CROP, gg.CROP, gg.CROP, gg.CROP, gg.CROP]], canopy=[[0, 1, 9, 2, 1]])
        self.assertEqual(k.tolist(), [[gg.CROP, gg.TREE, gg.TREE, gg.TREE, gg.CROP]])

    def test_lines(self):
        base = [[gg.BUILT, gg.GRASS, gg.GRASS, gg.TREE, gg.WATER, gg.GRASS]]
        line = [[1, 1, 2, 3, 3, 0]]
        k, r = self.regions(base, line=line, lkinds=(gg.PATH_AUTO, gg.TREE, gg.WATER), canopy=[[255, 255, 255, 12,
                                                                                                255, 255]])
        self.assertEqual(k.tolist(), [[gg.CONCRETE, gg.PATH, gg.TREE, gg.WATER, gg.WATER, gg.GRASS]],
                         "a footway: concrete in town, an earth path elsewhere; a hedge; a stream over trees")
        self.assertEqual(r[0, 3], r[0, 4], "a line's texels: one region per kind")

    def test_surfaces_and_lots(self):
        base = [[gg.LAWN, gg.TREE, gg.WATER, gg.LAWN, gg.GRASS, gg.TREE]]
        poly = [[1, 1, 1, 2, 2, 3]]
        k, _ = self.regions(base, poly, (gg.TURF, gg.LOT, gg.ORCHARD), canopy=[[9, 255, 255, 255, 255, 1]])
        self.assertEqual(k.tolist(), [[gg.TURF, gg.TURF, gg.WATER, gg.BUILT, gg.BUILT, gg.ORCHARD]],
                         "a pitch over the park (no canopy trees on it); commercial land's lawns paved; an orchard")


class GenGroundTest(unittest.TestCase):
    def test_border_takes_each_regions_own_colour(self):
        n, col = 96, 48
        s2 = blurred_edge(n, col, G, D)
        kind = np.where(np.arange(n)[None, :] < col, gg.GRASS, gg.TREE).astype(np.uint8).repeat(n, 0)
        out = gen(s2, kind).astype(np.float64)
        row = out[n // 2]
        # the photo's smear: the texels either side of the border are nearly the same; generated, each is its own
        self.assertLess(np.abs(s2[n // 2, col - 2].astype(float) - s2[n // 2, col + 1]).sum(), 70)
        self.assertLess(np.abs(row[col - 2] - G).sum(), 40, row[col - 2])
        self.assertLess(np.abs(row[col + 1] - D).sum(), 30, row[col + 1])
        # inside, the photo itself (with the kind's detail)
        self.assertLess(np.abs(out[:, 5].mean(0) - G).sum(), 15)

    def test_unknown_kind_is_the_photo(self):
        rng = np.random.default_rng(3)
        s2 = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
        out = gen(s2, np.zeros((64, 64), np.uint8))
        np.testing.assert_array_equal(out, s2)

    def test_water_is_flat(self):
        n = 96
        rng = np.random.default_rng(5)
        s2 = np.full((n, n, 3), (30, 50, 60), np.uint8)
        boats = rng.random((n, n)) < 0.04
        s2[boats] = (230, 230, 230)
        out = gen(s2, np.full((n, n), gg.WATER, np.uint8)).astype(np.float64)
        inner = out[20:-20, 20:-20]
        self.assertLess(inner[..., 0].std(), 4.0, "the boats are not the water's colour")
        self.assertLess(np.abs(inner.mean((0, 1)) - (30, 50, 60)).sum(), 12)

    def test_world_locked_detail(self):
        # two blocks 32 texels apart over the same ground: the same texels where both have them
        n, sh = 96, 32
        s2 = np.full((n, n, 3), (100, 120, 70), np.uint8)
        kind = np.full((n, n), gg.GRASS, np.uint8)
        a = gen(s2, kind, ox=500.0, oy=900.0)
        b = gen(s2, kind, ox=500.0 + sh, oy=900.0)
        np.testing.assert_array_equal(a[:, sh:], b[:, :n - sh])
        self.assertGreater(a[..., 1].std(), 1.0, "it has some detail")

    def test_overlapping_blocks_agree(self):
        # (Codex review of 41d74b61: windows clipped to each block drew the same ground in other colours where blocks
        # overlap.) A world of varying photo, regions with borders and water; two squares 37 texels apart, each with
        # the HALO round it: the texels both keep are the same bits
        rng = np.random.default_rng(21)
        W, n, halo = 420, 160, int(gg.HALO_M)
        x = np.arange(W)
        s2 = np.stack([(x[None, :] + 30 + 0 * x[:, None]) % 256, (x[:, None] * 3 // 4 + 20 + 0 * x[None, :]) % 256,
                       rng.integers(0, 60, (W, W))], -1).astype(np.uint8)
        kinds = [gg.GRASS, gg.TREE, gg.CROP, gg.WATER, gg.LAWN, gg.PAVED, gg.BUILT]
        kind = rng.choice(kinds, (W // 12 + 1, W // 9 + 1))
        kind = kind.repeat(12, 0).repeat(9, 1)[:W, :W].astype(np.uint8)
        kind[100:140, 50:300] = gg.WATER
        region = kind.astype(np.uint32) + 16 * ((np.arange(W)[None, :] // 70) % 3).astype(np.uint32)
        canopy = np.where(kind == gg.TREE, rng.integers(3, 25, (W, W)), 255).astype(np.uint8)

        def block(r0, c0):
            sl = (slice(r0 - halo, r0 + n + halo), slice(c0 - halo, c0 + n + halo))
            out = gen(s2[sl], kind[sl], region[sl], canopy[sl], ox=float(c0 - halo), oy=float(-(r0 - halo)))
            return out[halo:halo + n, halo:halo + n]
        a, b = block(70, 70), block(70 + 13, 70 + 37)
        np.testing.assert_array_equal(a[13:, 37:], b[:n - 13, :n - 37])
        # without the halo (the block's own square alone, as 41d74b61 did) they differ
        a0 = gen(s2[70:230, 70:230], kind[70:230, 70:230], region[70:230, 70:230], canopy[70:230, 70:230],
                 ox=70.0, oy=-70.0)
        b0 = gen(s2[83:243, 107:267], kind[83:243, 107:267], region[83:243, 107:267], canopy[83:243, 107:267],
                 ox=107.0, oy=-83.0)
        self.assertFalse(np.array_equal(a0[13:, 37:], b0[:n - 13, :n - 37]), "the test sees the old fault")

    def test_same_bits_on_any_threads(self):
        n = 160
        rng = np.random.default_rng(11)
        s2 = rng.integers(20, 200, (n, n, 3), dtype=np.uint8)
        kind = rng.choice([gg.GRASS, gg.TREE, gg.CROP, gg.WATER, gg.LAWN, gg.PAVED], (n // 8, n // 8)).astype(
            np.uint8).repeat(8, 0).repeat(8, 1)
        canopy = np.where(kind == gg.TREE, rng.integers(3, 30, (n, n)), 255).astype(np.uint8)
        outs = []
        try:
            for t in (1, 2, 3, 8):
                EF.set_native_threads(t)
                outs.append(gen(s2, kind, canopy=canopy))
        finally:
            EF.set_native_threads(0)
        for o in outs[1:]:
            np.testing.assert_array_equal(o, outs[0])

    def test_bad_shapes(self):
        with self.assertRaises(ValueError):
            gen(np.zeros((8, 8, 3), np.uint8), np.zeros((9, 9), np.uint8))


class _Feats:
    """A stand-in for a mapgeom.Features of polygons given in ENU metres (polygons() needs no more)."""

    def __init__(self, rings_by_feature, classes):
        self.ids = np.array([f"id{i}" for i in range(len(classes))])
        self.kind = np.full(len(classes), 3, np.uint8)
        self.attrs = {"class": np.array(classes)}
        self._parts = rings_by_feature

    def __len__(self):
        return len(self.ids)

    def xy_in(self, frame):
        return None

    def parts(self, i, xy=None):
        return [[np.asarray(r, np.float64) for r in self._parts[i]]]


class PolygonsTest(unittest.TestCase):
    def test_land_use_and_water(self):
        # a 64 m square at 1 m texels from (0, 0): a farm over most of it, a park inside, a pond on top
        lu = _Feats([[square(0, 0, 60, 60)], [square(10, 10, 20, 20)], [square(30, 30, 40, 40)]],
                    ["farmland", "park", "residential"])
        water = _Feats([[square(40, 40, 50, 50)]], ["lake"])
        poly, kinds, dirs, phases = gg.polygons(None, 0.0, 0.0, 64.0, 64, [("land_use", lu), ("water", water)])
        self.assertEqual(dirs.shape, (3, 2))
        self.assertEqual(len(phases), 3)
        self.assertEqual(kinds.tolist(), [gg.CROP, gg.LAWN, gg.WATER], "the largest first; residential has no kind")
        self.assertEqual(kinds[poly[64 - 15, 15] - 1], gg.LAWN, "row 0 = north: y 15 m is row 49")
        self.assertEqual(kinds[poly[64 - 45, 45] - 1], gg.WATER)
        self.assertEqual(kinds[poly[64 - 35, 35] - 1], gg.CROP)
        self.assertEqual(poly[0, 63], 0)


class ChainTest(unittest.TestCase):
    def test_only_sentinel2(self):
        kept = gg.chain(imagery.DEFAULT_CHAIN)
        self.assertEqual(kept, ("eox2023", "eox2016"))
        self.assertEqual(gg.chain(("bngaer", "md6in", "penn", "bngs2", "naip", "usgs", "eox2023", "eox2016")),
                         ("bngs2", "eox2023", "eox2016"), "no 1 m source left (the CDN's aerial packs neither)")
        for name in gg.SOURCES:
            self.assertEqual(imagery.SOURCES[name].min_zoom, 0)
            self.assertLessEqual(imagery.SOURCES[name].max_zoom, 14, "10 m sources only")
        self.assertEqual(gg.chain(()), gg.SOURCES)

    def test_switch(self):
        for v, on in (("on", True), ("1", True), ("off", False), ("", False)):
            with mock.patch.dict(os.environ, {"BNE_GEN_GROUND": v}):
                self.assertEqual(importlib.reload(gg).ON, on, v)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("BNE_GEN_GROUND", None)
            self.assertFalse(importlib.reload(gg).ON, "off by default")


class KeysTest(unittest.TestCase):
    """The switch makes other base photos, far textures and blocks: it is in the keys they are kept under."""

    def fresh(self):
        with revisit._lock:
            revisit._switches.clear()
            revisit._code.clear()

    def keys(self, env):
        with mock.patch.dict(os.environ, env):
            if "BNE_GEN_GROUND" not in env:
                os.environ.pop("BNE_GEN_GROUND", None)
            self.fresh()
            import decisions
            return (revisit.key("basephoto", 39.28, -76.59, 0.0, 0.0, 1024, 1.0, 1024, 2048),
                    revisit.switches_key(), decisions.vkey({"overture": "r1"})[0])

    def test_in_the_keys(self):
        self.addCleanup(self.fresh)
        off = self.keys({})
        on = self.keys({"BNE_GEN_GROUND": "on"})
        for a, b, what in zip(off, on, ("base photo (code_key)", "switches_key", "decision archives' vkey")):
            self.assertNotEqual(a, b, what)
        self.assertNotIn("BNE_GEN_GROUND", revisit.NOT_SWITCHES)


class _Im:
    def render(self, frame, x0, y0, size, px, pending=None):
        return np.full((px, px, 3), 100, np.uint8), {"eox2023": px * px}


class _MD:
    """Map data whose cells hold the layers in `have` (each read as empty)."""

    def __init__(self, have=()):
        self.have = set(have)

    def has(self, key, layer):
        return layer in self.have

    def cell(self, key, layers=None, kind=None):
        if not all(layer in self.have for layer in layers or ()):
            raise OSError("not here")
        return types.SimpleNamespace(layers={})


class RefreshTest(unittest.TestCase):
    """(Codex review of 41d74b61) a photo made while some of its map data was not on disk is marked (GEN_PENDING) and
    made again once it is: earthd.Server.base_ready waits for genground.data_ready."""

    def test_missing_data_marks_the_photo(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)
        import geodesy
        frame = geodesy.EnuFrame(40.877, -77.566)
        pending = set()
        rgb, _ = gg.render(_Im(), frame, -64.5, -64.5, 128.0, 128, _MD(), None, None, pending)
        self.assertEqual(rgb.shape, (128, 128, 3))
        self.assertIn(gg.GEN_PENDING, pending)
        self.assertFalse(gg.data_ready(_MD(), frame, -64.5, -64.5, 128.0))
        self.assertFalse(gg.data_ready(_MD(set(gg.LAYERS) - {"canopy"}), frame, -64.5, -64.5, 128.0))
        self.assertTrue(gg.data_ready(_MD(gg.LAYERS), frame, -64.5, -64.5, 128.0))
        pending = set()
        gg.render(_Im(), frame, -64.5, -64.5, 128.0, 128, None, None, None, pending)
        self.assertNotIn(gg.GEN_PENDING, pending, "no map data at all (none to wait for)")

    def test_server_redoes_it(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import earthd
        from test_server import LAT, LON, ServerHarness

        class Harness(ServerHarness):
            near_visual = "detail"
            with_imagery = True
            cells = 64
            block = 1024

            def runTest(self):
                pass
        h = Harness()
        h.setUp()
        self.addCleanup(h.tearDown)
        h.hello()
        aid = h.prepare_anchor(1, LAT, LON)["anchorId"]
        s = h.server
        anchor = s.anchors[aid]
        key = (aid, 0.0, 0.0)
        md = _MD({"buildings", "roads"})        # (the photo's own data on disk; the generated ground's not all)
        s.mapdata = md
        with mock.patch.object(earthd.genground, "ON", True):
            s.base_stale.discard(key)
            self.assertIsNotNone(s.write_base(anchor, 0.0, 0.0, rev=1))
            self.assertIn(gg.GEN_PENDING, s.base_pending[key])
            self.assertIn(key, s.base_stale, "made again later")
            self.assertFalse(s.base_ready(anchor, 0.0, 0.0), "not before its map data is here")
            md.have = set(gg.LAYERS)
            self.assertTrue(s.base_ready(anchor, 0.0, 0.0), "then base_refresh makes it again")
            s.write_base(anchor, 0.0, 0.0, rev=2)
            self.assertNotIn(gg.GEN_PENDING, s.base_pending[key])
            self.assertNotIn(key, s.base_stale)


class RoadsRetryTest(unittest.TestCase):
    """(Codex round 3) a cold block whose roads' fetch fails carries both marks (PHOTO_ROADS_PENDING, GEN_PENDING):
    base_ready returned at the roads' check before the generated ground's data_ready, so standing still never asked
    again and never gave up. Now the retries run first, and roads it gave up on count as done; without the generated
    ground's mark the roads' check is as before."""

    def test_both_marks_failed_roads(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import surface as surfacemod
        from test_server import LAT, LON, ServerHarness

        class Harness(ServerHarness):
            near_visual = "detail"
            with_imagery = True
            cells = 64
            block = 1024

            def runTest(self):
                pass
        h = Harness()
        h.setUp()
        self.addCleanup(h.tearDown)
        h.hello()
        aid = h.prepare_anchor(1, LAT, LON)["anchorId"]
        s = h.server
        anchor = s.anchors[aid]
        key = (aid, 0.0, 0.0)
        s.mapdata = _MD(set(gg.LAYERS) - {"roads"})        # (the roads' fetch keeps failing)
        now = [0.0]
        with mock.patch.object(gg, "_now", lambda: now[0]):
            s.base_pending[key] = frozenset({surfacemod.PHOTO_ROADS_PENDING, gg.GEN_PENDING})
            ready = None
            for step in range(0, 2000, 10):
                now[0] = float(step)
                if s.base_ready(anchor, 0.0, 0.0) and ready is None:
                    ready = step
            asks = [r[0] for (k, layer), r in gg._retries.items() if layer == "roads"]
            self.assertTrue(asks and min(asks) == gg.RETRY_TRIES, "asked again with the backoff, up to the give-up")
            self.assertEqual(ready, 1530, "then final: not stale for good")
            # switch off (no generated ground mark): the roads' check as before, never done without the roads
            s.base_pending[key] = frozenset({surfacemod.PHOTO_ROADS_PENDING})
            self.assertFalse(s.base_ready(anchor, 0.0, 0.0))


class WriteBaseTest(unittest.TestCase):
    """earthd.Server.write_base runs the generator on the imagery's render with the switch on (and not with it off, or
    for a quick stand-in)."""

    def test_write_base(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import earthd
        from test_server import LAT, LON, ServerHarness

        class Harness(ServerHarness):
            near_visual = "detail"
            with_imagery = True
            cells = 64
            block = 1024

            def runTest(self):
                pass
        h = Harness()
        h.setUp()
        self.addCleanup(h.tearDown)
        h.hello()
        aid = h.prepare_anchor(1, LAT, LON)["anchorId"]
        s = h.server
        anchor = s.anchors[aid]
        calls = []
        real = gg.render

        def spy(im, frame, x0, y0, size, px, *a, **k):
            got = real(im, frame, x0, y0, size, px, *a, **k)
            calls.append(got[0].shape)
            return got
        with mock.patch.object(earthd.genground, "render", spy):
            with mock.patch.object(earthd.genground, "ON", False):
                self.assertIsNotNone(s.write_base(anchor, 0.0, 0.0))
            self.assertEqual(calls, [], "off: the photo as before")
            with mock.patch.object(earthd.genground, "ON", True):
                self.assertIsNotNone(s.write_base(anchor, 0.0, 0.0, rev=1))
                self.assertEqual(calls, [(earthd.BASE_RENDER_PX, earthd.BASE_RENDER_PX, 3)])
                s.write_base(anchor, 0.0, 0.0, rev=2, quick=True)
        self.assertEqual(len(calls), 1 + (0 if s.mapdata is not None else 1),
                         "a quick stand-in without its map data on disk: no generator")


if __name__ == "__main__":
    unittest.main()
