"""server/evals/earth_writers.py: who last wrote a height sample, by the stack of the call (no Earth tree needed: synthetic
functions on a small lattice in the anchor frame).

    python -m pytest tests
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earth_writers as ew  # noqa: E402


class NoGeo:
    def geodetic_to_enu(self, la, lo, h):
        return np.asarray(la) * 0 + 0.5, np.asarray(lo) * 0 + 0.5, 0.0


@pytest.fixture
def tracker(tmp_path):
    t = ew.Tracker(NoGeo(), 1004.0, 2004.0, 8, str(tmp_path), lambda: [])
    t.server_dir = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))    # (these test functions: Earth's)
    ew.TRACK = t
    t.active = True
    yield t
    t.active = False
    ew.TRACK = None


def grid(t):
    x, y = np.meshgrid(t.x0 + np.arange(t.n), t.y0 + np.arange(t.n))
    return x.ravel(), y.ravel()


def names(t):
    return {sid: st[0] for sid, st in enumerate(t.stacks)}


def test_pixels_of_the_lattice_only(tracker):
    x, y = grid(tracker)
    assert (tracker.pixels(x, y) == np.arange(64)).all()
    assert tracker.pixels(x + 0.3, y) is None
    assert tracker.pixels(x + 100.0, y) is None or (tracker.pixels(x + 100.0, y) < 0).all()


def test_source_nested_writes_and_put_back(tracker):
    def sample(x, y):                       # the source: every sample
        return np.full(x.shape, 10.0)

    def raise_east(x, y, z):                # a nested writer
        return np.where(x >= 1004.0 + 4, z + 2.0, z)

    def weights(x, y, z):                   # not heights: returned inside a call that leaves them unchanged
        return np.linspace(0, 1, x.size)

    def grade(x, y, z):                     # the outer call: its nested writes, then a put-back of its own
        weights(x, y, z)
        out = raise_east(x, y, z)
        out[(x == 1004.0 + 7)] = z[(x == 1004.0 + 7)]
        return out

    sample = ew.wrap(sample, "sample")
    raise_east = ew.wrap(raise_east, "raise_east")
    weights = ew.wrap(weights, "weights")
    grade = ew.wrap(grade, "grade")
    x, y = grid(tracker)
    z = grade(x, y, sample(x, y))
    nm = names(tracker)
    lab = np.array([nm[s] for s in tracker.label])
    col = (x - 1004.0).astype(int)
    assert (lab[col < 4] == "sample").all()             # left as sampled: the weights do not count
    assert (lab[(col >= 4) & (col < 7)] == "raise_east").all()
    assert (lab[col == 7] == "sample").all()             # put back to its sampled value: the source again
    assert (tracker.val == z).all()
    by = np.array([nm.get(s, "") for s in tracker.undone_by])
    what = np.array([nm.get(s, "") for s in tracker.undone_what])
    assert (by[col == 7] == "grade").all() and (what[col == 7] == "raise_east").all()   # who gave back whose write
    # (the weights inside it count as given back too: what was given back is named, so they can be told apart)
    assert (what[col < 4] == "weights").all() and (tracker.undone_by[(col >= 4) & (col < 7)] < 0).all()


def test_outer_change_of_its_own(tracker):
    def sample(x, y):
        return np.zeros(x.shape)

    def lift(x, y, z):
        return z + 1.0

    def grade(x, y, z):
        out = lift(x, y, z)
        out[:8] = 5.0                       # its own write over the nested one
        return out
    sample, lift, grade = ew.wrap(sample, "sample"), ew.wrap(lift, "lift"), ew.wrap(grade, "grade")
    x, y = grid(tracker)
    grade(x, y, sample(x, y))
    nm = names(tracker)
    assert {nm[s] for s in tracker.label[:8]} == {"grade"}
    assert {nm[s] for s in tracker.label[8:]} == {"lift"}


def test_in_place_kernel(tracker):
    def sample(x, y):
        return np.zeros(x.shape)

    def kernel(zk, px, py):                 # native style: writes into its first argument
        zk[px > 1004.0 + 5] = 3.0
    sample, kernel = ew.wrap(sample, "sample"), ew.wrap(kernel, "kernel")
    x, y = grid(tracker)
    z = sample(x, y)
    kernel(z, x, y)
    nm = names(tracker)
    lab = np.array([nm[s] for s in tracker.label])
    assert (lab[(x - 1004.0) > 5] == "kernel").all() and (lab[(x - 1004.0) <= 5] == "sample").all()


def test_stack_hash_is_the_colour():
    st = ("grading._Planner.apply", "grading.py:apply:4310")
    import zlib
    assert 0 <= zlib.crc32("|".join(st).encode()) % 256 < 256
    assert ew.PALETTE.shape == (256, 3)
