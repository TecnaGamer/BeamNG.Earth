"""staticmaps: the species maps decoded once a machine, memory-mapped by every process."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import staticmaps  # noqa: E402


class MappedTest(unittest.TestCase):
    def test_decoded_once_then_mapped(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:     # (Windows: a mapped file stays)
            src = os.path.join(d, "map.bin")
            with open(src, "wb") as f:
                f.write(b"v1")
            calls = []

            def decode():
                calls.append(1)
                return np.arange(12, dtype=np.uint16).reshape(3, 4)
            with mock.patch.dict(os.environ, {staticmaps.DIR_ENV: os.path.join(d, "maps")}):
                a = staticmaps.mapped(src, decode)
                b = staticmaps.mapped(src, decode)
                self.assertEqual(len(calls), 1, "decoded once, then read from the .npy")
                np.testing.assert_array_equal(a, decode())
                np.testing.assert_array_equal(b, a)
                self.assertEqual(b.dtype, np.uint16)
                self.assertFalse(b.flags.writeable, "read-only: the processes share its pages")
                with open(src, "wb") as f:
                    f.write(b"v2")
                staticmaps.mapped(src, decode)
                self.assertEqual(len(calls), 3, "a changed file, decoded again (its own .npy)")
                self.assertEqual(len([n for n in os.listdir(os.path.join(d, "maps")) if n.endswith(".npy")]), 2)
                del a, b

    def test_folder_not_writable_decodes(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "map.bin")
            with open(src, "wb") as f:
                f.write(b"x")
            blocker = os.path.join(d, "file")
            with open(blocker, "wb") as f:
                f.write(b"")
            with mock.patch.dict(os.environ, {staticmaps.DIR_ENV: os.path.join(blocker, "maps")}):
                a = staticmaps.mapped(src, lambda: np.ones(3, np.uint8))
            np.testing.assert_array_equal(a, np.ones(3, np.uint8))

    def test_species_maps_values_unchanged(self):
        import ecoregion
        import leaftype
        import treegenus
        la, lo = np.array([52.0, 62.0, 43.3, -33.9]), np.array([20.0, 26.0, 17.0, 151.2])
        before = (leaftype._bilinear(la, lo, 0), ecoregion.eco_many(la, lo), treegenus.shares(la, lo))
        with mock.patch.object(staticmaps, "mapped", lambda path, decode: np.array(decode(), np.float64)
                               if path.endswith(".npz") else np.array(decode())):
            leaftype._grids.clear()
            ecoregion._grid = None
            treegenus._data = None
            after = (leaftype._bilinear(la, lo, 0), ecoregion.eco_many(la, lo), treegenus.shares(la, lo))
        leaftype._grids.clear()
        ecoregion._grid = None
        treegenus._data = None
        for x, y in zip(before, after):
            np.testing.assert_array_equal(np.asarray(x), np.asarray(y))


if __name__ == "__main__":
    unittest.main()
