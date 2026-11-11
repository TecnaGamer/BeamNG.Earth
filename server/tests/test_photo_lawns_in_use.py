"""A block in use never has its .ter patched (Server._photo_lawns / _base_later): the engine watches the file, reloaded
it when patched and froze in its TextureCooker (Billings, 2026-11-11)."""
import os
import sys
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import earthd  # noqa: E402


class PhotoLawnsInUseTest(unittest.TestCase):
    def test_a_committed_block_is_left_alone(self):
        fake = SimpleNamespace(hm_committed=deque([(1, 0.0, 384.0)]), out_dir="/nowhere")
        with mock.patch.object(earthd.surfacemod, "photo_lawns") as pl:
            earthd.Server._photo_lawns(fake, (1, 0.0, 384.0), "hm_1_0_384_5.ter", ["m0"], None)
            pl.assert_not_called()
            earthd.Server._photo_lawns(fake, (1, 0.0, 0.0), "hm_1_0_0_6.ter", ["m0"], None)
            pl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
