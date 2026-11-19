"""The roadnet kernels read a numpy argument as numpy does whatever its layout (roadnet::pyobj::CArr: Codex on a6b93359):
a byte-strided view (strides not a whole number of elements, as a float64 (2, 2) of strides (24, 12)), an unaligned
one and a reversed one give the same answers as their contiguous copies. rust-numpy's own views divided the byte
strides by the item size and read other bytes."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import sweden  # noqa: E402


def odd(a, step=12, offset=4):
    """a's values in a view of byte strides `step` per element (step * ncols per row) from an unaligned offset."""
    a = np.ascontiguousarray(a)
    item = a.dtype.itemsize
    step = max(step, item + 1) if step % item == 0 else step
    if a.ndim == 1:
        strides = (step,)
    else:
        strides = (step * a.shape[1], step)
    buf = np.zeros(offset + step * max(a.size, 1) + item, np.uint8)
    v = np.ndarray(a.shape, a.dtype, buffer=buf, offset=offset, strides=strides)
    v[...] = a
    assert np.array_equal(v, a, equal_nan=a.dtype.kind == "f") and (v.size <= 1 or not v.flags.c_contiguous)
    return v


def same(a, b):
    if isinstance(a, tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, np.ndarray):
        return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()
    return (a == b) or (a != a and b != b)


class LayoutTest(unittest.TestCase):
    def test_kernels_read_odd_layouts_as_numpy(self):
        rng = random.Random(9)
        for k in range(200):
            m = rng.randint(1, 12)
            s = np.sort(np.array([rng.uniform(0, 300) for _ in range(m)]))
            ll = np.c_[np.cumsum([rng.uniform(-1e-3, 1e-3) for _ in range(m)]) - 76.6,
                       np.cumsum([rng.uniform(-1e-3, 1e-3) for _ in range(m)]) + 39.28]
            d = np.array([rng.uniform(-5, 5) for _ in range(m)])
            for view in (odd, lambda a: odd(a, 20, 3), lambda a: np.ascontiguousarray(a[::-1])[::-1]):
                self.assertTrue(same(fast._ef.lane_ease(view(s), 300.0, (30.0, 45.0), (0.25, 0.5)),
                                     fast._ef.lane_ease(s, 300.0, (30.0, 45.0), (0.25, 0.5))), k)
                self.assertTrue(same(fast._ef.lane_offset(view(ll), view(d)), fast._ef.lane_offset(ll, d)), k)
                self.assertTrue(same(fast._ef.ring_radius(view(ll)), fast._ef.ring_radius(ll)), k)
                x, y = ll[:, 0] + 95.0, ll[:, 1] + 20.0
                self.assertTrue(same(fast._ef.outline_contains(sweden.OUTLINE, view(x), view(y)),
                                     fast._ef.outline_contains(sweden.OUTLINE, x, y)), k)
                po, ro, co = (np.array(v, np.int64) for v in ([0, 1], [0, 1], [0, m]))
                self.assertTrue(same(fast._ef.layer_lengths(view(po), view(ro), view(co), view(ll)),
                                     fast._ef.layer_lengths(po, ro, co, ll)), k)


if __name__ == "__main__":
    unittest.main()
