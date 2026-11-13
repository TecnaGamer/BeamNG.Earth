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


    def test_a_block_in_use_built_again_gets_a_new_photo_name(self):
        import contextlib
        import itertools
        got = []
        fake = SimpleNamespace(hm_committed=deque([(1, 0.0, 384.0)]), base_rev=itertools.count(1),
                               imagery=SimpleNamespace(urgent=contextlib.nullcontext),
                               base_revisit_key=lambda a, cx, cy: None,
                               write_base=lambda a, cx, cy, rev=0, photo=None, quick=False: got.append(rev) or "x")
        earthd.Server._base_photo(fake, SimpleNamespace(id=1), 0.0, 384.0)       # in use: a new revision
        earthd.Server._base_photo(fake, SimpleNamespace(id=1), 0.0, 0.0)         # not: r0
        self.assertEqual(got, [1, 0])


if __name__ == "__main__":
    unittest.main()
