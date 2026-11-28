"""natural: OpenStreetMap's mapped natural features (BNE_NATURAL_*): mapped trees and tree rows placed where they stand
(their species' stock shapes, their size; the canopy's tops that are them dropped, none on a building, out of a road's
corridor), forests' leaf types, natural ground on the blocks, rock faces along mapped cliffs, lake floors from the depth
bands; nothing differs while every switch is off."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast  # noqa: E402
import mapcells  # noqa: E402
import natural  # noqa: E402
import vegetation as veg  # noqa: E402
from test_vegetation import KEY, Feats, FakeMapData  # noqa: E402

ef = natural.require()


def feats(kind, rows, cls, tags=None, surface=None):
    f = Feats(kind, rows, cls)
    f.attrs["tags"] = np.array([json.dumps(t) if t else "" for t in (tags or [None] * len(rows))], object)
    if surface is not None:
        f.attrs["surface"] = np.array(surface, object)
    return f


class TagsTest(unittest.TestCase):
    def test_species_and_sizes(self):
        tags = [{"genus": "Platanus", "species": "Platanus × hispanica", "height": "15", "circumference": "1.10"},
                {"species": "Pinus sylvestris", "height": "12 m", "diameter_crown": "6"},
                {"genus": "Quercus", "species": "Quercus ilex"},
                {"leaf_type": "needleleaved"},
                {"leaf_type": "broadleaved", "leaf_cycle": "evergreen", "height": "40 ft"},
                {"natural": "tree", "height": "tall"},
                {"taxon": "Betula pendula", "circumference": "110"}]
        slot, leaf, h, cr, circ = ef.natural_tree_info([json.dumps(t) for t in tags] + ["", "not json"])
        names = [natural.SPECIES[s] if s < len(natural.SPECIES) else None for s in slot]
        self.assertEqual(names, ["deciduous", "pine", "holm_oak", "conifer", "deciduous", None, "birch", None, None])
        self.assertEqual(h[0], 15.0)
        self.assertEqual(h[1], 12.0)
        self.assertAlmostEqual(float(h[4]), 12.192, places=3)
        self.assertTrue(np.isnan(h[5]))
        self.assertEqual(cr[1], 6.0)
        self.assertAlmostEqual(float(circ[0]), 1.1, places=5)
        self.assertAlmostEqual(float(circ[6]), 1.1, places=5)           # (110: centimetres)
        self.assertEqual(leaf[4], natural.LEAF_BROAD | natural.CYCLE_EVERGREEN)


class SwitchesOffTest(unittest.TestCase):
    def test_off_keys_and_groups_as_before(self):
        with mock.patch.multiple(natural, TREES=False, LEAF=False, SEASON=False, GROUND=False, CLIFFS=False,
                                 BATHY=False, VEG_ON=False):
            self.assertEqual(natural.veg_key(), "")
            self.assertEqual(natural.sea_key(), "")
            self.assertEqual(veg.groups_list(), list(veg.BASE_GROUPS))
        self.assertEqual(len(veg.BASE_GROUPS), 26, "the base groups' numbers never move (vine_row, PR #1501, the 26th)")
        self.assertEqual(veg.GROUPS[:26], veg.BASE_GROUPS)
        with mock.patch.multiple(natural, TREES=True, VEG_ON=True):
            self.assertTrue(natural.veg_key().startswith("-nat"))
            self.assertEqual(veg.groups_list(), list(veg.GROUPS))

    def test_off_cell_unchanged(self):
        """With the switches off the cell is the same items as with a land layer present (it is never read)."""
        md = FakeMapData()
        a = veg.Vegetation(md, None).cell(KEY)
        md2 = FakeMapData()
        lon, lat = md2.lonlat(100, 100)
        md2.cd.layers["land"] = feats(1, [[(float(lon), float(lat))]], ["tree"], [{"genus": "Pinus"}])
        b = veg.Vegetation(md2, None).cell(KEY)
        for f in ("lon", "lat", "height", "group", "rank", "yaw", "scale"):
            np.testing.assert_array_equal(getattr(a, f), getattr(b, f))


class MappedTreesTest(unittest.TestCase):
    def _md(self, extra=(), rows=()):
        md = FakeMapData()
        pts, tags = [], []
        lon, lat = md.lonlat(100, 100)                       # on the free crown: that crown's tree, a pine
        pts.append([(float(lon), float(lat))])
        tags.append({"genus": "Pinus", "height": "11"})
        lon, lat = md.lonlat(300, 300)                       # in the building: left out
        pts.append([(float(lon), float(lat))])
        tags.append({"genus": "Tilia"})
        lon, lat = md.lonlat(450, 60)                        # in the open, no canopy: its species' usual height
        pts.append([(float(lon), float(lat))])
        tags.append({"genus": "Quercus", "species": "Quercus robur"})
        for p, t in extra:
            pts.append(p)
            tags.append(t)
        f = feats(1, pts, ["tree"] * len(pts), tags)
        if rows:
            r = feats(2, [r for r, _ in rows], ["tree_row"] * len(rows), [t for _, t in rows])
            f = _join(f, r)
        md.cd.layers["land"] = f
        return md

    def test_placed_deduplicated_never_in_buildings(self):
        md = self._md()
        with mock.patch.multiple(natural, TREES=True, VEG_ON=True):
            it = veg.Vegetation(md, None).cell(KEY)
        trees = it.take(it.kind == veg.KIND_TREE)
        lon, lat = md.lonlat(100, 100)
        d = np.hypot((trees.lon - lon) * 85000, (trees.lat - lat) * 111000)
        near = np.flatnonzero(d < 8.0)
        self.assertEqual(len(near), 1, "one tree at the free crown: the mapped one, not the canopy's too")
        self.assertEqual(veg.GROUPS[trees.group[near[0]]], "pine")
        self.assertAlmostEqual(float(trees.height[near[0]]), 11.0, places=3)
        self.assertLess(d[near[0]], 0.5, "where it is mapped")
        blon, blat = md.lonlat(300, 300)
        self.assertGreater(np.hypot((trees.lon - blon) * 85000, (trees.lat - blat) * 111000).min(), 8.0)
        olon, olat = md.lonlat(450, 60)
        k = np.argmin(np.hypot((trees.lon - olon) * 85000, (trees.lat - olat) * 111000))
        self.assertEqual(veg.GROUPS[trees.group[k]], "oak")
        self.assertAlmostEqual(float(trees.height[k]), natural.DEFAULT_H["oak"], places=3)

    def test_on_a_road_moved_out_of_it(self):
        md = FakeMapData()
        lon, lat = md.lonlat(100, 400)                       # on the residential road's centreline
        extra = [([(float(lon), float(lat))], {"genus": "Acer", "height": "8"})]
        md = self._md(extra)
        with mock.patch.multiple(natural, TREES=True, VEG_ON=True):
            it = veg.Vegetation(md, None).cell(KEY)
        trees = it.take((it.kind == veg.KIND_TREE) & (np.abs(it.height - 8.0) < 1e-3))
        self.assertEqual(len(trees), 1)
        off = abs(trees.lat[0] - lat) * 111000
        self.assertGreater(off, veg.ROAD_HALF_WIDTH_M["residential"], "off the carriageway")

    def test_rows_spaced_and_not_doubled(self):
        md = FakeMapData()
        a, b = md.lonlat(470, 150), md.lonlat(470, 350)      # ~200 px of open ground, no canopy
        row = [(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))]
        m = md.lonlat(470, 250)                              # one of its trees mapped on its own too
        md = self._md([([(float(m[0]), float(m[1]))], {"genus": "Tilia", "height": "7"})],
                      rows=[(row, {"natural": "tree_row", "genus": "Platanus", "height": "12"})])
        with mock.patch.multiple(natural, TREES=True, VEG_ON=True):
            it = veg.Vegetation(md, None).cell(KEY)
        on = it.take((it.kind == veg.KIND_TREE) & (np.abs(it.lat - a[1]) * 111000 < 2.0))
        length = abs(b[0] - a[0]) * 111320 * np.cos(np.radians(a[1]))
        self.assertGreater(len(on), length / natural.ROW_SPACING_M - 3)
        self.assertLess(len(on), length / natural.ROW_SPACING_M + 2)
        x = np.sort(on.lon) * 111320 * np.cos(np.radians(a[1]))
        self.assertGreater(np.diff(x).min(), 3.0, "no two on top of each other")

    def test_sources_mapped_vs_inferred(self):
        """Each item's evidence ranks (natural.S_*): the mapped trees 1, the canopy's 2, the scatter's 4; kept in the
        cell's file; none while the switches are off."""
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.multiple(natural, TREES=True, VEG_ON=True):
            it = veg.Vegetation(self._md(), d).cell(KEY)
            again = veg.Vegetation(self._md(), d).cell(KEY)        # (from the file)
        self.assertIsNotNone(it.source)
        np.testing.assert_array_equal(it.source, again.source)
        np.testing.assert_array_equal(it.hsource, again.hsource)
        pine = np.flatnonzero(it.group == veg.GROUPS.index("pine"))
        self.assertEqual(it.source[pine].tolist(), [natural.S_MAPPED])
        self.assertEqual(it.hsource[pine].tolist(), [natural.S_MAPPED], "its height from its tag")
        oak = np.flatnonzero(it.group == veg.GROUPS.index("oak"))
        self.assertEqual(it.hsource[oak].tolist(), [natural.S_DEFAULT], "no tag, no canopy: its species' usual")
        canopy = (it.kind == veg.KIND_TREE) & (it.source != natural.S_MAPPED)
        self.assertTrue(canopy.any() and (it.source[canopy] == natural.S_MEASURED).all())
        bush = it.kind == veg.KIND_BUSH
        self.assertTrue(set(it.source[bush].tolist()) <= {natural.S_MEASURED, natural.S_PRIOR})
        off = veg.Vegetation(FakeMapData(), None).cell(KEY)
        self.assertIsNone(off.source)

    def test_deterministic(self):
        with mock.patch.multiple(natural, TREES=True, VEG_ON=True):
            a = veg.Vegetation(self._md(), None).cell(KEY)
            b = veg.Vegetation(self._md(), None).cell(KEY)
        for f in ("lon", "lat", "height", "group", "rank", "yaw", "scale"):
            np.testing.assert_array_equal(getattr(a, f), getattr(b, f))

    def test_street_furniture_trees_near_mapped_left_out(self):
        rows = [["deciduous", 0.0, 0.0, 0, 0, 1, 8, 0.1], ["deciduous", 30.0, 0.0, 0, 0, 1, 8, 0.1],
                ["bench", 0.5, 0.0, 0, 0, 1, 1, 0.1]]
        with mock.patch.object(natural, "mapped_tree_xy", lambda *a: (np.array([[1.0, 2.0]]), np.array([[1.0, 2.0]]))):
            out = natural.off_mapped(rows, None, 0, 0, None, ("deciduous",))
        self.assertEqual([r[1] for r in out], [30.0, 0.5])


def _join(a, b):
    out = Feats(1, [], [])
    out.kind = np.concatenate([a.kind, b.kind])
    out.rows = a.rows + b.rows
    keys = sorted(set(a.attrs) | set(b.attrs))
    out.attrs = {k: np.concatenate([a.attrs.get(k, np.array([None] * len(a.kind), object)),
                                    b.attrs.get(k, np.array([None] * len(b.kind), object))]) for k in keys}
    out.lonlat = np.concatenate([a.lonlat, b.lonlat])
    out.coord_off = np.cumsum([0] + [len(r) for r in out.rows]).astype(np.int64)
    out.ring_off = np.arange(len(out.rows) + 1, dtype=np.int64)
    out.part_off = np.arange(len(out.rows) + 1, dtype=np.int64)
    return out


class LeafTest(unittest.TestCase):
    def test_smaller_polygon_wins(self):
        big = [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01), (0.0, 0.0)]
        small = [(0.004, 0.004), (0.006, 0.004), (0.006, 0.006), (0.004, 0.006), (0.004, 0.004)]
        f = feats(3, [small, big], ["forest", "wood"],
                  [{"leaf_type": "needleleaved"}, {"leaf_type": "broadleaved", "leaf_cycle": "deciduous"}])
        got = natural.forest_codes([f])
        x0, y1 = veg._to3857(0.0, 0.01)
        x1, y0 = veg._to3857(0.01, 0.0)
        r = natural.code_raster(got, (100, 100), (x0, y1, (x1 - x0) / 100, (y1 - y0) / 100))
        self.assertEqual(r[50, 50], natural.LEAF_NEEDLE)
        self.assertEqual(r[10, 10], natural.LEAF_BROAD | natural.CYCLE_DECIDUOUS)

    def test_share_and_groups(self):
        share = veg._leaf_share(np.array([1, 2, 3, 0, 5], np.uint8), np.full(5, 0.3))
        np.testing.assert_array_equal(share, [0.1, 0.9, 0.5, 0.3, 0.1])
        g = np.array([veg.G_DECIDUOUS] * 4 + [veg.G_CONIFER], np.uint8)
        code = np.array([natural.CYCLE_EVERGREEN | 1, natural.CYCLE_DECIDUOUS | 1, natural.CYCLE_EVERGREEN | 1,
                         natural.CYCLE_DECIDUOUS | 1, natural.CYCLE_DECIDUOUS | 2], np.uint8)
        lat = np.array([45.0, 45.0, 10.0, -40.0, 45.0])
        with mock.patch.object(natural, "SEASON", True):
            out = veg._leaf_groups(g, code, lat)
        self.assertEqual([veg.GROUPS[v] for v in out], ["holm_oak", "leafoff_n", "deciduous", "leafoff_s", "conifer"])
        with mock.patch.object(natural, "SEASON", False):
            out = veg._leaf_groups(g, code, lat)
        self.assertEqual([veg.GROUPS[v] for v in out], ["holm_oak", "deciduous", "deciduous", "deciduous", "conifer"])


class GroundTest(unittest.TestCase):
    def _grid(self):
        lat, lon = np.meshgrid(np.linspace(0.001, 0.0, 101), np.linspace(0.0, 0.001, 101), indexing="ij")
        return lat, lon

    def test_polygons_and_cliff_band(self):
        import surface
        lat, lon = self._grid()
        idx = np.full(lat.shape, surface.LOOSE, np.uint8)      # (bare ground: the mapped ground refines it)
        idx[:12, :] = surface.GRASS                            # (a meadow along the north edge)
        idx[:, 95:] = surface.FOREST                           # (a forest along the east edge)
        sq = [(0.0002, 0.0002), (0.0005, 0.0002), (0.0005, 0.0005), (0.0002, 0.0005), (0.0002, 0.0002)]
        rock = [(0.0, 0.00092), (0.001, 0.00092), (0.001, 0.001), (0.0, 0.001), (0.0, 0.00092)]     # over the meadow
        gl = [(0.0006, 0.0006), (0.0009, 0.0006), (0.0009, 0.0009), (0.0006, 0.0009), (0.0006, 0.0006)]
        wet = [(0.0002, 0.0006), (0.0004, 0.0006), (0.0004, 0.0008), (0.0002, 0.0008), (0.0002, 0.0006)]
        cliff = [(0.0001, 0.0001), (0.0009, 0.0001)]          # west to east: its foot to the south (the right)
        f = _join(feats(3, [sq, gl, wet, rock], ["beach", "glacier", "wetland", "bare_rock"],
                        [{"surface": "sand"}, {}, {"wetland": "marsh"}, {}], [None, None, None, None]),
                  feats(2, [cliff], ["cliff"], [{}]))
        f.attrs.setdefault("surface", np.array([None] * len(f.kind), object))
        with mock.patch.multiple(natural, GROUND=True, CLIFFS=True):
            out = surface.paint_natural(idx, lat, lon, [f])
        r, c = lambda la: int(round((0.001 - la) / 0.001e-2)), lambda lo: int(round(lo / 0.001e-2))
        self.assertEqual(out[r(0.00035), c(0.00035)], surface.SAND)
        self.assertEqual(out[r(0.00075), c(0.00075)], surface.SNOW)
        self.assertEqual(out[r(0.0007), c(0.0003)], surface.MUD)
        self.assertEqual(out[r(0.00007), c(0.0005)], surface.ROCK, "the cliff's foot")
        self.assertEqual(out[r(0.00018), c(0.0005)], surface.LOOSE, "not far up behind its top")
        self.assertEqual(out[r(0.00096), c(0.0005)], surface.GRASS, "a coarse rock polygon keeps the meadow it covers")
        self.assertEqual(out[r(0.00096), c(0.00097)], surface.FOREST)
        self.assertEqual(out[r(0.0007), c(0.00097)], surface.FOREST, "the glacier's ice never over the forest")

    def test_pebble_beach_is_loose(self):
        import surface
        lat, lon = self._grid()
        idx = np.full(lat.shape, surface.DUSTY, np.uint8)
        sq = [(0.0002, 0.0002), (0.0005, 0.0002), (0.0005, 0.0005), (0.0002, 0.0005), (0.0002, 0.0002)]
        f = feats(3, [sq], ["beach"], [{"surface": "pebblestone"}], ["pebblestone"])
        with mock.patch.multiple(natural, GROUND=True, CLIFFS=False):
            out = surface.paint_natural(idx, lat, lon, [f])
        self.assertEqual(out[65, 35], surface.LOOSE)


class CliffTest(unittest.TestCase):
    def test_pieces_where_it_drops_and_off_roads(self):
        line = np.array([[0.0, 0.0], [200.0, 0.0]])        # west to east: the foot to the south
        st, ids = ef.natural_cliff_stations(line, np.array([0, 2], np.int64), 3.0)
        st = np.asarray(st)
        self.assertTrue(np.allclose(st[:, 2:4], [0.0, -1.0]), "the face toward the right (south)")
        drop = np.where(st[:, 0] < 100.0, 12.0, 0.5)        # a cliff on the west half only
        blocked = np.zeros((100, 300), np.uint8)
        road = np.zeros((100, 300), np.uint8)
        road[:, 90:110] = 1                                 # a road across it at x 40-60 m
        import roads as roadsmod
        shapes = [float(v) for sh in roadsmod.CLIFF_SHAPES for v in sh]
        x, y, yaw, h, jit = ef.natural_cliff_pieces(st, ids, drop, shapes, roadsmod.CLIFF_TALL_M,
                                                    [2.0, 1.0, 0.3, 0.55, 33.0], blocked, road,
                                                    [-50.0, 50.0, 1.0, 1.0], 1.0)
        x = np.asarray(x)
        self.assertTrue(len(x) >= 4)
        self.assertTrue((x < 110.0).all(), "none where the ground does not drop")
        self.assertTrue(np.all(np.asarray(y) > 0.0), "pivots behind the face (north of the line)")
        self.assertFalse(((x > 40.0) & (x < 60.0)).any(), "none on the road")
        yw = np.asarray(yaw) % 360.0
        self.assertTrue(np.all(np.minimum(yw, 360.0 - yw) <= 10.5), "facing south (yaw 0), jittered")
        pr = np.asarray(ef.natural_cliff_probes(x, np.asarray(y), np.asarray(yaw, np.float64),
                                                np.asarray(h, np.float64), shapes, roadsmod.CLIFF_TALL_M, 15.0))
        m = len(x)
        self.assertTrue(np.all(pr[m:2 * m, 1] < pr[:m, 1]), "the foot probe south of the face")
        z = np.concatenate([np.full(m, 100.0), np.full(m, 98.0), np.full(m, 110.0)])
        zc, hc = ef.natural_cliff_fit(np.asarray(h, np.float64), z, np.asarray(jit, np.float64), shapes,
                                      roadsmod.CLIFF_TALL_M, 1.0, 0.3, 33.0)
        self.assertTrue(np.all(np.asarray(hc) > 12.0))
        self.assertTrue(np.all(np.asarray(zc) < 110.0))


class BathyTest(unittest.TestCase):
    def test_bands_ramp_and_nan_outside(self):
        def sq(a, b):
            return [(a, a), (b, a), (b, b), (a, b), (a, a)]
        f = Feats(3, [sq(0.0, 0.002), sq(0.0003, 0.0017)])
        import seacarve
        x0, y0 = seacarve._merc(0.002, 0.0)
        ox, oy = int(x0) - 10, int(y0) - 10
        n = 120
        t = np.asarray(ef.natural_bathy_target(f.lonlat, f.coord_off, f.ring_off, f.part_off,
                                               np.array([0, 1], np.int64), np.array([10.0, 50.0]), ox, oy, n, 16,
                                               32.0))
        self.assertTrue(np.isnan(t[2, 2]), "outside every band")
        cx, cy = seacarve._merc(0.001, 0.001)
        self.assertAlmostEqual(float(t[int(cy) - oy, int(cx) - ox]), 50.0, places=3)
        ex, ey = seacarve._merc(0.001, 0.00032)              # just inside the deep band's edge
        v = float(t[int(ey) - oy, int(ex) - ox])
        self.assertGreater(v, 10.0)
        self.assertLess(v, 30.0, "ramping from the band above it")


if __name__ == "__main__":
    unittest.main()
