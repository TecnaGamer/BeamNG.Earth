"""Concurrent map readers share completed decoding, including errors and eviction races."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells
import mapdata
import mapgeom
import netfetch

KEY = mapcells.CellKey(14, 16145, 9998)


class SingleFlightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.md = mapdata.MapData(self.tmp.name, fetcher=netfetch.Fetcher(offline=True))
        self.md.rail_tags = self.md.lane_tags = None
        self.addCleanup(self.md.close)
        self.addCleanup(lambda: self.md.pool.shutdown(wait=True))

    @staticmethod
    def arrays(layer):
        a = mapgeom.empty(layer).to_arrays("")
        a["provenance"] = "{}"
        return a, True

    def wait_shared(self, n):
        end = time.monotonic() + 5
        while self.md.cell_stats()["coalesced"] < n and time.monotonic() < end:
            time.sleep(0.001)
        self.assertEqual(self.md.cell_stats()["coalesced"], n)

    def test_cold_readers_share_one_decode_and_warm_reader_reuses_it(self):
        release = threading.Event()
        def load(key, layer, kind):
            if not release.wait(5):
                raise TimeoutError("test producer was not released")
            return self.arrays(layer)
        with patch.object(self.md, "layer_arrays", side_effect=load) as io, ThreadPoolExecutor(8) as callers:
            jobs = [callers.submit(self.md.cell, KEY, ("roads",)) for _ in range(8)]
            try:
                self.wait_shared(7)
            finally:
                release.set()
            cells = [f.result(5) for f in jobs]
            warm = self.md.cell(KEY, ("roads",))
        self.assertEqual(io.call_count, 1)
        self.assertTrue(all(c.layers["roads"] is warm.layers["roads"] for c in cells))
        self.assertEqual(sum(c.stats["roads"]["shared"] for c in cells), 7)
        self.assertEqual(self.md._loading, {})
        self.assertGreater(self.md.cell_stats()["wait_s"], 0)

    def test_failed_producer_wakes_all_readers_and_next_request_retries(self):
        release = threading.Event()
        def fail(*args):
            release.wait(5)
            raise ValueError("bad source")
        with patch.object(self.md, "layer_arrays", side_effect=fail) as io, ThreadPoolExecutor(2) as callers:
            jobs = [callers.submit(self.md.cell, KEY, ("roads",)) for _ in range(2)]
            try:
                self.wait_shared(1)
            finally:
                release.set()
            for future in jobs:
                with self.assertRaisesRegex(ValueError, "bad source"):
                    future.result(5)
        self.assertEqual(io.call_count, 1)
        self.assertEqual(self.md._loading, {})
        with patch.object(self.md, "layer_arrays", return_value=self.arrays("roads")):
            self.assertIsNotNone(self.md.cell(KEY, ("roads",)).layers["roads"])

    def test_overlapping_layer_orders_do_not_wait_on_each_other(self):
        both = threading.Barrier(2)
        def load(key, layer, kind):
            both.wait(5)
            return self.arrays(layer)
        with patch.object(self.md, "layer_arrays", side_effect=load) as io, ThreadPoolExecutor(2) as callers:
            a = callers.submit(self.md.cell, KEY, ("roads", "buildings"))
            b = callers.submit(self.md.cell, KEY, ("buildings", "roads"))
            a, b = a.result(5), b.result(5)
        self.assertEqual(io.call_count, 2)
        for layer in ("roads", "buildings"):
            self.assertIs(a.layers[layer], b.layers[layer])

    def test_eviction_does_not_make_waiters_decode_again(self):
        release = threading.Event()
        def load(key, layer, kind):
            release.wait(5)
            return self.arrays(layer)
        remember = self.md._remember
        def immediately_evict(k, entry):
            remember(k, entry)
            with self.md._mem_lock:
                self.md._mem.clear()
                self.md._mem_bytes = 0
        with patch.object(self.md, "layer_arrays", side_effect=load) as io, \
             patch.object(self.md, "_remember", side_effect=immediately_evict), ThreadPoolExecutor(2) as callers:
            jobs = [callers.submit(self.md.cell, KEY, ("roads",)) for _ in range(2)]
            try:
                self.wait_shared(1)
            finally:
                release.set()
            a, b = [f.result(5) for f in jobs]
        self.assertIs(a.layers["roads"], b.layers["roads"])
        self.assertEqual(io.call_count, 1)

    def test_cache_lock_is_not_held_during_tags_and_neighbors_are_lazy(self):
        def tags(*args):
            acquired = self.md._mem_lock.acquire(timeout=2)
            self.assertTrue(acquired)
            if acquired:
                self.md._mem_lock.release()
        neighbor = lambda: self.fail("neighbors must not be expanded during decoding")
        with patch.object(self.md, "layer_arrays", return_value=self.arrays("roads")), \
             patch.object(self.md, "_lanes", side_effect=tags), \
             patch.object(mapdata.gradesep, "neighbours_hook", return_value=neighbor):
            got = self.md.cell(KEY, ("roads",)).layers["roads"]
        self.assertIs(got._bnge_neighbours, neighbor)


if __name__ == "__main__":
    unittest.main()
