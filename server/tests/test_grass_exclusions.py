"""GroundCover polygon vetoes: cached data only, eligibility and topology preserved."""
import os
import sys
import tempfile
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grass_exclusions as gx  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402


def grids(n=12, start=0):
    r, c = np.mgrid[0:n, start:start + n]
    return 41.1 + r * 0.00001, -104.9 + c * 0.00001


def ring(c0, r0, c1, r1):
    return np.array([[-104.9 + c * 0.00001, 41.1 + r * 0.00001]
                     for c, r in ((c0, r0), (c1, r0), (c1, r1), (c0, r1), (c0, r0))])


def features(layer, parts, attrs=None, kind=None):
    """One polygon per feature, each represented by [outer ring, optional interior rings]."""
    rings = [r for part in parts for r in part]
    return mapgeom.Features(
        layer, np.array([str(i) for i in range(len(parts))]),
        np.array(kind if kind is not None else [3] * len(parts), np.uint8),
        np.arange(len(parts) + 1, dtype=np.int64),
        np.cumsum([0] + [len(part) for part in parts]).astype(np.int64),
        np.cumsum([0] + [len(r) for r in rings]).astype(np.int64),
        np.concatenate(rings) if rings else np.empty((0, 2)),
        attrs={k: np.array([str(x or "") for x in v]) for k, v in (attrs or {}).items()})


class CachedMap:
    """Any attempt to use the data-fetching cell API is a test failure."""
    def __init__(self, root):
        self.root = root
        self._mem_lock = threading.Lock()
        self._mem = {}
        self.reads = 0

    def path(self, key, layer):
        self.reads += 1
        return os.path.join(self.root, str(key.x) + "_" + str(key.y) + "_" + layer + ".npz")

    def cell(self, *args, **kwargs):
        raise AssertionError("vegetation exclusions must never fetch a cell")

    def store(self, layer, feats):
        key = mapcells.cell_of(41.1, -104.9)
        np.savez_compressed(self.path(key, layer), **feats.to_arrays(""))


class GrassExclusionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.md = CachedMap(self.tmp.name)
        self.lat, self.lon = grids()
        self.eligible = np.ones(self.lat.shape, bool)

    def mask(self):
        return gx.blocked(self.md, self.lat, self.lon, self.eligible)

    def test_buildings_and_water_veto_only_eligible_samples(self):
        self.md.store("buildings", features("buildings", [[ring(1, 1, 4, 4)]]))
        self.md.store("water", features("water", [[ring(7, 7, 10, 10)]]))
        self.eligible[3, 3] = False       # asphalt, a hole or any other existing excluded surface
        out = self.mask()
        self.assertTrue(out[2, 2])
        self.assertTrue(out[8, 8])
        self.assertFalse(out[3, 3])
        self.assertFalse(out[6, 6])
        self.assertFalse((out & ~self.eligible).any())

    def test_polygon_holes_and_overlapping_features(self):
        outer, hole = ring(1, 1, 10, 10), ring(3, 3, 7, 7)
        self.md.store("water", features("water", [[outer, hole]]))
        first = self.mask()
        self.assertTrue(first[2, 2])
        self.assertFalse(first[5, 5], "an island inside a lake is not water")
        self.md.store("buildings", features("buildings", [[ring(4, 4, 6, 6)]]))
        self.assertTrue(self.mask()[5, 5], "a later polygon's interior ring cannot erase another veto")

    def test_explicit_unvegetated_attributes_and_grassy_parking(self):
        f = features("land_use", [[ring(1, 1, 4, 4)], [ring(7, 1, 10, 4)], [ring(1, 7, 4, 10)]],
                     {"class": ["quarry", "parking", "park"], "surface": [None, "grass", "concrete"]})
        self.md.store("land_use", f)
        out = self.mask()
        self.assertTrue(out[2, 2], "an explicit quarry")
        self.assertFalse(out[2, 8], "preserve an explicitly grassy parking area")
        self.assertTrue(out[8, 2], "concrete overrides a broad green land-use class")

    def test_broad_land_uses_and_nonpolygon_features_are_not_vetoes(self):
        f = features("infrastructure", [[ring(1, 1, 4, 4)], [ring(7, 7, 10, 10)]],
                     {"class": ["industrial", "substation"], "surface": ["", "concrete"]}, kind=[3, 2])
        self.md.store("infrastructure", f)
        self.assertFalse(self.mask().any(), "industrial land can have grass; a line has no polygon footprint")

    def test_missing_and_corrupt_cache_do_not_fetch_or_add_exclusions(self):
        self.assertFalse(self.mask().any())
        key = mapcells.cell_of(41.1, -104.9)
        with open(self.md.path(key, "buildings"), "wb") as f:
            f.write(b"not an npz file")
        self.assertFalse(self.mask().any())
        self.assertTrue(self.eligible.all(), "the input eligibility is untouched")

    def test_existing_decoded_mapdata_cache_is_reused(self):
        key = mapcells.cell_of(41.1, -104.9)
        f = features("buildings", [[ring(1, 1, 4, 4)]])
        self.md._mem[(str(key), "buildings")] = (f, None, 1000)
        self.assertTrue(self.mask()[2, 2], "the decoded cell works without a disk layer")

    def test_no_eligible_grass_does_no_cache_work(self):
        self.eligible[:] = False
        self.assertFalse(self.mask().any())
        self.assertEqual(self.md.reads, 0)

    def test_overlap_of_shifted_blocks_keeps_the_same_vetoes(self):
        self.md.store("land_use", features("land_use", [[ring(3.25, 1.25, 9.25, 9.25)]],
                                           {"subtype": ["bare_rock"]}))
        a = self.mask()
        lat, lon = grids(start=4)
        b = gx.blocked(self.md, lat, lon, np.ones(lat.shape, bool))
        np.testing.assert_array_equal(a[:, 4:], b[:, :8])

    def test_shape_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            gx.blocked(self.md, self.lat, self.lon, self.eligible[:2])


if __name__ == "__main__":
    unittest.main()
