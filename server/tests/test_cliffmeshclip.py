"""Cliff-only native tile clipping preserves the established seam/attribute rules."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import unittest
import numpy as np
import fast
from roadmesh import _clip_tris


class CliffMeshClipTest(unittest.TestCase):
    def test_matches_reference_for_all_attribute_channels_and_boundaries(self):
        rng = np.random.default_rng(92)
        p = rng.uniform(-3, 3, (90, 3))
        p[:6] = [[0,0,0],[1,0,1],[1,1,2],[0,1,3],[-1,0,0],[2,0,0]]
        t = np.vstack(([[0,1,2],[0,2,3],[4,0,5],[0,0,0]], rng.integers(0,len(p),(180,3))))
        for channels in (0, 2, 11, 15):
            attrs = rng.random((len(p),channels))
            for box in ((0,0,1,1),(-4,-4,4,4),(9,9,10,10),(0,0,0,1)):
                with self.subTest(channels=channels,box=box):
                    expected = _clip_tris(p,t,attrs,np.zeros(len(t),np.int32),box)
                    actual = fast._ef.cliff_mesh_clip(p,t,attrs,box)
                    np.testing.assert_array_equal(actual[1],expected[1])
                    np.testing.assert_allclose(actual[0],expected[0],rtol=0,atol=1e-14)
                    np.testing.assert_allclose(actual[2],expected[2],rtol=0,atol=1e-14)

    def test_empty_and_invalid_inputs(self):
        p=np.zeros((3,3)); a=np.zeros((3,11)); t=np.empty((0,3),np.int64)
        result=fast._ef.cliff_mesh_clip(p,t,a,(0,0,1,1))
        self.assertEqual(result[1].shape,(0,3))
        np.testing.assert_array_equal(result[0],p)
        for bad in (np.array([[-1,0,1]],np.int64), np.array([[0,1,3]],np.int64)):
            with self.assertRaises(ValueError): fast._ef.cliff_mesh_clip(p,bad,a,(0,0,1,1))
        for box in ((1,0,0,1),(0,0,np.nan,1)):
            with self.assertRaises(ValueError): fast._ef.cliff_mesh_clip(p,t,a,box)
        with self.assertRaises(ValueError): fast._ef.cliff_mesh_clip(p,t,a[:2],(0,0,1,1))
