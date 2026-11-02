"""Revisit diagnostics belong to this job; queue duration starts at process entry."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker as bw
import geotiles
import revisit


class TimingTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(bw, "_s", SimpleNamespace(cache_dir="unused", md=None))
        p.start()
        self.addCleanup(p.stop)
        bw._tile_stages.last = {"old_job": 12.5}

    def test_geotile_revisit_does_not_inherit_old_work(self):
        gx, gy = geotiles.tile_of(-36.85, 174.76)
        with patch.object(bw, "_geo_content_key", return_value="key"), \
             patch.object(revisit, "load", return_value=({}, {})):
            bw._geo_content(gx, gy, 15, "unused", "/x", True, True, False, False, 0, None, "NZ")
        self.assertEqual(set(bw.last_tile_stages()), {"cache_restore"})
        self.assertGreaterEqual(bw.last_tile_stages()["cache_restore"], 0)

    def test_legacy_revisit_does_not_inherit_old_work(self):
        with patch.object(revisit, "load", return_value=({}, {})):
            result = bw._revisit_tile("roads", "key", 1, 0, 0, "unused", "/x", "tile.json",
                lambda: self.fail("cache hit must not build"), None, lambda path, extra: path)
        self.assertEqual(result, "/x/tile.json")
        self.assertEqual(set(bw.last_tile_stages()), {"cache_restore"})

    def test_process_entry_timestamp_and_dependency_deltas(self):
        md = SimpleNamespace(cell_stats=unittest.mock.Mock(side_effect=[
            {"loads": 3, "coalesced": 1, "wait_s": 2.0},
            {"loads": 5, "coalesced": 4, "wait_s": 2.5}]))
        bw._s.md = md
        # Actual entry precedes stack-watch setup and all build stages. No executor state is consulted.
        with patch.object(bw.time, "perf_counter", side_effect=range(20, 40)), \
             patch.object(bw, "_stack_watch"):
            r = bw.roads_geo(1, 0, 0, 15, 1, 1, 15, "unused", "/x", True, 0, True, False, True,
                             1/256, False, content=False, skin=False, submitted_at=7)
        self.assertEqual(r["worker"]["started_at"], 20)
        self.assertEqual(r["worker"]["queue_s"], 13)
        self.assertGreater(r["worker"]["finished_at"], 20)
        self.assertEqual(r["dependencies"], {"loads": 2, "coalesced": 3, "wait_s": 0.5})

    def test_skin_clock_resumes_after_nested_content_restore(self):
        outer, mark = bw._stage_clock()
        _, nested = bw._stage_clock()
        nested("cache_restore")
        mark("inputs")
        self.assertEqual(bw.last_tile_stages(), outer)
        self.assertEqual(set(outer), {"inputs"})

    def test_optional_dependency_stats_preserve_build_failure_without_worker_state(self):
        with patch.object(bw, "_s", None), \
             patch.object(bw, "_stack_watch") as watch, \
             patch.object(bw, "_geo_content", side_effect=RuntimeError("original build failure")):
            with self.assertRaisesRegex(RuntimeError, "original build failure"):
                bw.roads_geo(1, 0, 0, 15, 65536, 65536, 15, "unused", "/vfs", True, 0,
                             True, False, False, 0, False, skin=False)
            watch.return_value.cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
