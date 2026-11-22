"""Road tile waits (2026-11-21): the products a road tile's build shares with the server's other processes and later
runs, and the batched kernels that replaced per-line Python on its critical path.

- roadsurf3's home-cell lane graphs kept on disk (LANES_DISK): read back only under the same key (format, code, home
  cell, border data, map data versions, lane kernel) with every read cell's roads file as it was; kept only when
  complete.
- railways.level_bridges: every short rail bridge's crossings in one kernel call, the same answers as one call per
  bridge.
- guardrails._overlaps_bridge_approach: the far-module shortcut gives the full test's answer.
- roadlayercache.store_later: a decode's write on the writer thread gives the file store gives, the layer's bytes
  copied before the decode returns.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import mapcells  # noqa: E402
import revisit  # noqa: E402
import roadsurf3  # noqa: E402


class FakeMd:
    """The MapData side roadsurf3's disk lane graphs read: product folders, file stamps, completeness, versions."""

    def __init__(self, root):
        self.root = root
        self.stamps = {}
        self.incomplete = set()
        self.vers = {"overture": "r1", "dem": "d1"}

    def roads_product_dir(self, key, name):
        return os.path.join(self.root, str(key).replace("/", "_") + "." + name)

    def roads_stamp(self, cell):
        return self.stamps.get(str(cell))

    def roads_complete(self, key, feats):
        return feats is not None and str(key) not in self.incomplete

    def versions(self):
        return dict(self.vers)


class LaneGraphsOnDisk(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lanesdisk")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.md = FakeMd(self.root)
        self.home = mapcells.CellKey(14, 3000, 6000)
        self.other = mapcells.CellKey(14, 3001, 6000)
        for k in (self.home, self.other):
            self.md.stamps[str(k)] = "100 1"
        self.feats = {str(self.home): SimpleNamespace(_road_revision=0), str(self.other): SimpleNamespace(_road_revision=0)}
        self.calls = 0
        self.fault = False
        p = patch.object(roadsurf3.lanegraph, "cell_lanes", side_effect=self._build)
        p.start()
        self.addCleanup(p.stop)
        lru = dict(roadsurf3._LANES_LRU)
        self.addCleanup(lambda: (roadsurf3._LANES_LRU.clear(), roadsurf3._LANES_LRU.update(lru)))
        self.addCleanup(setattr, roadsurf3, "LANES_DISK", roadsurf3.LANES_DISK)
        roadsurf3.LANES_DISK = True

    def _build(self, key, cell, memo, read_cells, events_only):
        self.calls += 1
        read_cells.extend((self.home, self.other))
        cell(self.home), cell(self.other)
        if self.fault:
            revisit.fault()
        return {"events": [{"node": 7, "kind": "diverge", "rows": [[0.0, 1.5, -2.25, 0.1]]}], "segs": [],
                "links": [], "frame": [45.5, -73.5], "cell": str(key), "n": self.calls}

    def cell_fn(self):
        fn = lambda k: self.feats.get(str(k))       # noqa: E731
        fn.lanes_md = self.md
        return fn

    def get(self, process_fresh=True):
        """_cell_lanes as a new process would call it (process_fresh: nothing in this process's memory)."""
        if process_fresh:
            roadsurf3._LANES_LRU.clear()
        with revisit.scope() as b:
            got = roadsurf3._cell_lanes(self.home, self.cell_fn(), {})
        return got, b

    def test_another_process_reads_the_kept_graph(self):
        first, b1 = self.get()
        second, b2 = self.get()
        self.assertEqual(self.calls, 1, "made once, read back by the next process")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertTrue(b1.complete and b2.complete)

    def test_a_read_cell_whose_file_changed_is_made_again(self):
        self.get()
        self.md.stamps[str(self.other)] = "101 2"
        got, _ = self.get()
        self.assertEqual(self.calls, 2)
        self.assertEqual(got["n"], 2)
        self.get()
        self.assertEqual(self.calls, 2, "kept again under the new stamp")

    def test_a_read_cell_gone_is_made_again(self):
        self.get()
        del self.md.stamps[str(self.other)]
        self.get()
        self.assertEqual(self.calls, 2)

    def test_each_key_part_counts(self):
        import countrybounds
        self.get()
        n = 1
        for change in (lambda: self.md.vers.update(dem="d2"),
                       lambda: patch.object(revisit, "code_key", return_value="other-code").start(),
                       lambda: patch.object(countrybounds, "signature", return_value="other-borders").start(),
                       lambda: patch.object(roadsurf3.lanegraph, "LANES_VERSION", 99999).start(),
                       lambda: patch.object(roadsurf3, "LANES_DISK_FORMAT", 99).start()):
            change()
            self.get()
            n += 1
            self.assertEqual(self.calls, n)
            self.get()
            self.assertEqual(self.calls, n, "the new key kept")
        patch.stopall()

    def test_short_reads_are_not_kept(self):
        self.fault = True
        _, b = self.get()
        self.assertFalse(b.complete)
        self.fault = False
        self.get()
        self.assertEqual(self.calls, 2, "made again: the short one was not kept")
        self.get()
        self.assertEqual(self.calls, 2)

    def test_an_incomplete_or_missing_read_cell_is_not_kept(self):
        self.md.incomplete.add(str(self.other))     # (its tags not joined, a stand-in)
        self.get()
        self.get()
        self.assertEqual(self.calls, 2)
        self.md.incomplete.clear()
        del self.feats[str(self.other)]             # (the painter's cell not there yet: None)
        self.get()
        self.get()
        self.assertEqual(self.calls, 4)

    def test_off_and_without_a_map_data_nothing_is_written(self):
        roadsurf3.LANES_DISK = False
        self.get()
        self.get()
        self.assertEqual(self.calls, 2)
        self.assertEqual(os.listdir(self.root), [])
        roadsurf3.LANES_DISK = True
        fn = lambda k: self.feats.get(str(k))       # noqa: E731
        roadsurf3._LANES_LRU.clear()
        roadsurf3._cell_lanes(self.home, fn, {})
        self.assertEqual(os.listdir(self.root), [])

    def test_kept_bytes_are_deterministic_and_floats_exact(self):
        self.get()
        d = self.md.roads_product_dir(self.home, "lanes")
        with open(os.path.join(d, "ok"), "rb") as fh:
            one = fh.read()
        shutil.rmtree(d)
        self.calls = 0
        got, _ = self.get()
        with open(os.path.join(d, "ok"), "rb") as fh:
            self.assertEqual(fh.read().replace(b'"n":1', b""), one.replace(b'"n":1', b""))
        back, _ = self.get()
        self.assertEqual(back["events"][0]["rows"][0][2], -2.25)
        self.assertEqual(repr(back), repr(got))

    def test_a_damaged_file_is_made_again(self):
        self.get()
        d = self.md.roads_product_dir(self.home, "lanes")
        with open(os.path.join(d, "ok"), "r+", encoding="utf-8") as fh:
            head = fh.readline()
            fh.seek(len(head))
            fh.write("{not json")
            fh.truncate()
        self.get()
        self.assertEqual(self.calls, 2)


class LayerWritesLater(unittest.TestCase):
    """roadlayercache.store_later: the same kept layer as store, written on the writer thread."""

    def setUp(self):
        import roadlayercache as rc
        self.rc = rc
        self.dir = tempfile.mkdtemp(prefix="rlc")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def layer(self):
        sys.path.insert(0, HERE)
        from test_roadlayercache import layer
        return layer()

    def test_the_same_files_as_store(self):
        a, b = os.path.join(self.dir, "a.dec"), os.path.join(self.dir, "b.dec")
        self.rc.store(a, "k1", self.layer(), [("14/1/2", "10 20")])
        written = []
        f = self.layer()
        self.rc.store_later(b, "k1", f, [("14/1/2", "10 20")], written.append)
        f.en[:] = -1.0                  # (changed after the decode returned: the write has its own copy)
        f.attrs["class"][0] = "motorway"
        self.assertTrue(self.rc.flush(30))
        for name in os.listdir(a):
            with open(os.path.join(a, name), "rb") as x, open(os.path.join(b, name), "rb") as y:
                self.assertEqual(x.read(), y.read(), name)
        self.assertEqual(len(written), 1)
        self.assertGreater(written[0], 0)
        self.assertFalse(self.rc.pending(b, "k1"))

    def test_off_writes_before_returning(self):
        with patch.object(self.rc, "ASYNC", False):
            d = os.path.join(self.dir, "c.dec")
            self.assertGreater(self.rc.store_later(d, "k1", self.layer(), []), 0)
            self.assertTrue(self.rc.has(d, "k1"))

    def test_unkept_layers_are_not_queued(self):
        f = self.layer()
        f.__dict__["_unknown_state"] = 1
        d = os.path.join(self.dir, "e.dec")
        self.assertEqual(self.rc.store_later(d, "k1", f, []), 0)
        self.assertTrue(self.rc.flush(30))
        self.assertFalse(os.path.exists(d))


class LevelBridgesBatched(unittest.TestCase):
    """railways.level_bridges' hits from one kernel call for all short bridges, against a call per bridge."""

    def test_same_hits_per_bridge_and_way(self):
        import fast
        import roads
        if not fast.AVAILABLE:
            self.skipTest("earthfast")
        rng = np.random.default_rng(11)
        for case in range(25):
            ways = [np.cumsum(rng.normal(0, 30, (int(rng.integers(2, 12)), 2)), axis=0) for _ in range(int(rng.integers(1, 40)))]
            bridges = [np.cumsum(rng.normal(0, 20, (int(rng.integers(1, 6)), 2)), axis=0) for _ in range(int(rng.integers(1, 15)))]
            want = [roads._crossings_by([b], ways) for b in bridges]
            got = [[[] for _ in ways] for _ in bridges]
            for g, hits in enumerate(roads._crossings_by(bridges, ways)):
                for pi, arc, pt in hits:
                    got[pi][g].append((0, arc, pt))
            for w, h in zip(want, got):
                self.assertEqual([[(p, a, tuple(x)) for p, a, x in hs] for hs in w],
                                 [[(p, a, tuple(x)) for p, a, x in hs] for hs in h], case)


class ApproachShortcut(unittest.TestCase):
    """guardrails._overlaps_bridge_approach's far-module shortcut never changes its answer."""

    def test_answers_unchanged(self):
        import guardrails
        rng = np.random.default_rng(5)
        hits = 0
        for case in range(3000):
            line = np.cumsum(rng.normal(0, 15, (int(rng.integers(2, 6)), 2)), axis=0)
            end = line[-1] if case % 2 else line[0]
            a = end + rng.normal(0, rng.choice([1.0, 3.0, 20.0, 60.0]), 2)
            b = a + rng.normal(0, 3.0, 2)
            run = {"line": line}
            got = guardrails._overlaps_bridge_approach(a, b, run)
            self.assertEqual(got, _approach_reference(guardrails, a, b, run), case)
            hits += got
        self.assertGreater(hits, 0, "some modules do continue a run")


def _approach_reference(g, a, b, run):
    """guardrails._overlaps_bridge_approach as it was before the shortcut (2026-11-21)."""
    import dnp
    line = np.asarray(run.get("line", ()), np.float64).reshape(-1, 2)
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    d = b - a
    length = float(dnp.hypot(d[0], d[1]))
    if len(line) < 2 or length < 1e-6:
        return False
    tangent = d / length
    samples = a[None, :] + np.linspace(0.1, 0.9, g.PARAPET_MATCH_SAMPLES)[:, None] * d[None, :]
    for at, inside in ((0, 1), (-1, -2)):
        edge = line[at]
        outward = edge - line[inside]
        edge_length = float(dnp.hypot(outward[0], outward[1]))
        if edge_length < 1e-6:
            continue
        outward /= edge_length
        if abs(float(dnp.dot(tangent, outward))) < g.PARAPET_PARALLEL_DOT:
            continue
        delta = samples - edge
        along = dnp.matmul(delta, outward)
        lateral = np.abs(delta[:, 0] * outward[1] - delta[:, 1] * outward[0])
        near = ((along >= -g.SPAN_TOL_M) & (along <= g.JOIN_LENGTH_M) & (lateral <= g.JOIN_LATERAL_M))
        if float(near.mean()) >= g.PARAPET_MATCH_SHARE:
            return True
    return False


if __name__ == "__main__":
    unittest.main()
