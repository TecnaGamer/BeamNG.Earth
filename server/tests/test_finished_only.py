"""imagery.Imagery.finished_only (a far-tile worker's texture with the server's analyses on, blockworker._imagery
tex["fin"]): the server's finished tiles on disk alone. One there and final is used; one missing, or with a
correction still to come, fails the render, and nothing is made from a raw tile (the uncorrected picture under the
server's names)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import imagery  # noqa: E402


class _Analysis:
    """A stand-in de-shadow: only its settings matter here (the finished tiles' directory, Imagery._fin_sig)."""
    mode, version, meta = "full", 7, {"k": 1}
    tone_lift = 0.5

    def bind(self, im):
        pass

    def poll(self, rk):
        return "wait"


class FinishedOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "imagery")
        self.server = imagery.Imagery(root, deshadow=_Analysis())
        self.src = self.server.chain[0]
        self.sigs = {s.name: self.server._fin_sig(s) for s in self.server.chain}
        self.worker = imagery.Imagery(root)
        self.worker._fin_sigs.update(self.sigs)
        self.worker.finished_only = True

    def keep(self, key, wait=()):
        rgb = np.full((imagery.TILE, imagery.TILE, 3), 90, np.uint8)
        rgb[0, 0] = (10, 200, 30)               # (not uniform: kept as a JPEG)
        self.server._fin_keep(key, imagery.Finished(rgb, True, False, None, False, tuple(wait)))

    def test_the_servers_directory(self):
        self.assertNotEqual(imagery.Imagery(self.server.cache_dir)._fin_path((self.src, 16, 1, 2)),
                            self.server._fin_path((self.src, 16, 1, 2)))     # (no analyses: another directory)
        self.assertEqual(self.worker._fin_path((self.src, 16, 1, 2)), self.server._fin_path((self.src, 16, 1, 2)))

    def test_final_tile_used(self):
        key = (self.src, 16, 100, 200)
        self.keep(key)
        f = self.worker.finished([key])[key]
        self.assertTrue(f.relit)

    def test_missing_tile_fails_without_the_raw(self):
        key = (self.src, 16, 100, 201)
        with mock.patch.object(self.worker, "tiles", side_effect=AssertionError("a raw tile read")):
            with self.assertRaises(imagery.ImageryError):
                self.worker.finished([key])
        self.assertFalse(os.path.exists(self.worker._fin_path(key)))

    def test_tile_with_a_correction_to_come_fails(self):
        key = (self.src, 16, 100, 202)
        self.keep(key, wait=(("cm", (3, 4)),))
        with mock.patch.object(self.worker, "tiles", side_effect=AssertionError("a raw tile read")):
            with self.assertRaises(imagery.ImageryError):
                self.worker.finished([key])


if __name__ == "__main__":
    unittest.main()
